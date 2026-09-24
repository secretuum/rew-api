from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from rew_api.config import Settings
from rew_api.models import (
    Organization,
    OrganizationSource,
    Project,
    Review,
    ReviewMedia,
    SyncRun,
    utcnow,
)
from rew_api.providers import ProviderFetchResult, ProviderRegistry, ResolvedSource


class SyncAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyncSummary:
    source_id: int
    status: str
    fetched_count: int
    created_count: int
    updated_count: int
    error: str | None = None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ReviewSyncService:
    def __init__(self, settings: Settings, registry: ProviderRegistry) -> None:
        self.settings = settings
        self.registry = registry

    def sync_source(self, session: Session, source_id: int) -> SyncSummary:
        source = session.scalar(
            select(OrganizationSource)
            .options(selectinload(OrganizationSource.organization))
            .where(OrganizationSource.id == source_id)
        )
        if source is None:
            raise LookupError(f"Source {source_id} not found")
        if not source.enabled or not source.organization.is_active:
            raise ValueError(f"Source {source_id} is disabled")

        now = utcnow()
        if source.sync_status == "running" and source.last_sync_started_at:
            age = now - _aware(source.last_sync_started_at)
            if age < timedelta(minutes=self.settings.sync_stale_after_minutes):
                raise SyncAlreadyRunning(f"Source {source_id} is already being synchronized")

        run = SyncRun(source=source, status="running", started_at=now)
        source.sync_status = "running"
        source.sync_error = None
        source.last_sync_started_at = now
        session.add(run)
        session.commit()

        provider = self.registry.get(source.provider)
        resolved = ResolvedSource(
            provider=source.provider,
            source_url=source.source_url,
            normalized_url=source.normalized_url,
            external_org_id=source.external_org_id,
        )

        try:
            result = provider.fetch_reviews(resolved, since=source.last_success_at)
            summary = self._persist_result(session, source, run, result)
        except Exception as exc:
            session.rollback()
            source = session.get(OrganizationSource, source_id)
            run = session.get(SyncRun, run.id)
            if source is None or run is None:
                raise

            finished = utcnow()
            source.failure_count += 1
            source.sync_status = "failed"
            source.sync_error = str(exc)[:4_000]
            source.last_sync_finished_at = finished
            retry_minutes = min(15 * (2 ** max(0, source.failure_count - 1)), 360)
            source.next_sync_at = finished + timedelta(minutes=retry_minutes)
            run.status = "failed"
            run.error = source.sync_error
            run.finished_at = finished
            session.commit()
            return SyncSummary(
                source_id=source_id,
                status="failed",
                fetched_count=0,
                created_count=0,
                updated_count=0,
                error=str(exc),
            )

        return summary

    def _persist_result(
        self,
        session: Session,
        source: OrganizationSource,
        run: SyncRun,
        result: ProviderFetchResult,
    ) -> SyncSummary:
        fetched_ids = [review.external_id for review in result.reviews]
        existing = {
            review.provider_review_id: review
            for review in session.scalars(
                select(Review)
                .options(selectinload(Review.media))
                .where(
                    Review.source_id == source.id,
                    Review.provider_review_id.in_(fetched_ids) if fetched_ids else False,
                )
            )
        }

        now = utcnow()
        created_count = 0
        updated_count = 0
        for incoming in result.reviews:
            stored = existing.get(incoming.external_id)
            is_existing = stored is not None
            if stored is None:
                stored = Review(
                    organization_id=source.organization_id,
                    source_id=source.id,
                    provider_review_id=incoming.external_id,
                    first_seen_at=now,
                )
                session.add(stored)
                created_count += 1
            else:
                updated_count += 1

            stored.author_name = incoming.author_name
            stored.author_avatar_url = incoming.author_avatar_url
            stored.published_at = incoming.published_at
            stored.edited_at = incoming.edited_at
            stored.rating = incoming.rating
            stored.text = incoming.text
            stored.raw_payload = incoming.raw_payload
            stored.business_reply_text = incoming.business_reply_text
            stored.business_reply_at = incoming.business_reply_at
            stored.is_visible = True
            stored.last_seen_at = now
            stored.media.clear()
            if is_existing:
                # Delete old rows before inserting replacement URLs so the
                # (review_id, url) unique constraint cannot see both versions.
                session.flush()
            for position, media in enumerate(incoming.media):
                stored.media.append(
                    ReviewMedia(
                        provider_media_id=media.external_id,
                        media_type=media.media_type,
                        url=media.url,
                        preview_url=media.preview_url,
                        sort_order=position,
                    )
                )

        if source.last_success_at is None and result.is_complete:
            unseen_query = select(Review).where(Review.source_id == source.id)
            if fetched_ids:
                unseen_query = unseen_query.where(Review.provider_review_id.not_in(fetched_ids))
            for unseen in session.scalars(unseen_query):
                unseen.is_visible = False

        finished = utcnow()
        status = "success" if result.is_complete else "partial"
        source.sync_status = status
        source.sync_error = None
        source.failure_count = 0
        source.last_sync_finished_at = finished
        source.next_sync_at = finished + timedelta(minutes=source.sync_interval_minutes)
        if result.is_complete:
            source.last_success_at = finished
        source.metrics = {
            **result.metrics,
            "fetched_count": len(result.reviews),
            "is_complete": result.is_complete,
        }

        run.status = status
        run.finished_at = finished
        run.fetched_count = len(result.reviews)
        run.created_count = created_count
        run.updated_count = updated_count
        session.commit()

        return SyncSummary(
            source_id=source.id,
            status=status,
            fetched_count=len(result.reviews),
            created_count=created_count,
            updated_count=updated_count,
        )

    def due_source_ids(self, session: Session, *, limit: int | None = None) -> list[int]:
        now = utcnow()
        query = (
            select(OrganizationSource.id)
            .join(OrganizationSource.organization)
            .join(Organization.project)
            .where(
                OrganizationSource.enabled.is_(True),
                Organization.is_active.is_(True),
                Project.is_active.is_(True),
                or_(
                    OrganizationSource.next_sync_at.is_(None),
                    OrganizationSource.next_sync_at <= now,
                ),
            )
            .order_by(OrganizationSource.organization_id, OrganizationSource.id)
        )
        if limit is not None:
            query = query.limit(limit)
        return list(session.scalars(query))

    def sync_due(
        self,
        session_factory: sessionmaker[Session],
        *,
        limit: int | None = None,
        delay_min_seconds: float | None = None,
        delay_max_seconds: float | None = None,
    ) -> list[SyncSummary]:
        with session_factory() as session:
            source_ids = self.due_source_ids(session, limit=limit)

        delay_min = (
            self.settings.sync_delay_min_seconds if delay_min_seconds is None else delay_min_seconds
        )
        delay_max = (
            self.settings.sync_delay_max_seconds if delay_max_seconds is None else delay_max_seconds
        )
        if delay_max < delay_min:
            raise ValueError("delay_max_seconds must be >= delay_min_seconds")

        summaries: list[SyncSummary] = []
        for index, source_id in enumerate(source_ids):
            with session_factory() as session:
                try:
                    summary = self.sync_source(session, source_id)
                except (SyncAlreadyRunning, ValueError, LookupError) as exc:
                    summary = SyncSummary(
                        source_id=source_id,
                        status="skipped",
                        fetched_count=0,
                        created_count=0,
                        updated_count=0,
                        error=str(exc),
                    )
                summaries.append(summary)

            if index + 1 < len(source_ids) and delay_max > 0:
                time.sleep(random.uniform(delay_min, delay_max))

        return summaries
