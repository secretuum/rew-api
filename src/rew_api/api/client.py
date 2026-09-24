from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from rew_api.api.dependencies import current_project, get_session
from rew_api.models import Organization, OrganizationSource, Project, Review
from rew_api.schemas import (
    BusinessReplyResponse,
    OrganizationResponse,
    ReviewMediaResponse,
    ReviewPage,
    ReviewResponse,
    SourceResponse,
)


router = APIRouter(prefix="/v1", tags=["reviews"])


def _source_response(source: OrganizationSource) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        provider=source.provider,
        source_url=source.source_url,
        external_org_id=source.external_org_id,
        enabled=source.enabled,
        sync_status=source.sync_status,
        sync_error=source.sync_error,
        last_success_at=source.last_success_at,
        next_sync_at=source.next_sync_at,
        metrics=source.metrics,
    )


@router.get("/organizations", response_model=list[OrganizationResponse])
def list_organizations(
    project: Project = Depends(current_project),
    session: Session = Depends(get_session),
) -> list[OrganizationResponse]:
    organizations = session.scalars(
        select(Organization)
        .options(selectinload(Organization.sources))
        .where(Organization.project_id == project.id, Organization.is_active.is_(True))
        .order_by(Organization.id)
    ).all()
    return [
        OrganizationResponse(
            id=organization.public_id,
            name=organization.name,
            sources=[_source_response(source) for source in organization.sources],
        )
        for organization in organizations
    ]


@router.get("/reviews", response_model=ReviewPage)
def list_reviews(
    organization_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    project: Project = Depends(current_project),
    session: Session = Depends(get_session),
) -> ReviewPage:
    query = (
        select(Review)
        .join(Review.organization)
        .join(Review.source)
        .options(
            selectinload(Review.media),
            selectinload(Review.source),
            selectinload(Review.organization),
        )
        .where(Organization.project_id == project.id, Review.is_visible.is_(True))
    )
    if organization_id:
        organization_exists = session.scalar(
            select(Organization.id).where(
                Organization.public_id == organization_id,
                Organization.project_id == project.id,
            )
        )
        if organization_exists is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        query = query.where(Organization.public_id == organization_id)
    if provider:
        query = query.where(OrganizationSource.provider == provider)

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    reviews = session.scalars(
        query.order_by(Review.published_at.desc(), Review.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [
        ReviewResponse(
            id=review.provider_review_id,
            organization_id=review.organization.public_id,
            provider=review.source.provider,
            author_name=review.author_name,
            author_avatar_url=review.author_avatar_url,
            published_at=review.published_at,
            edited_at=review.edited_at,
            rating=review.rating,
            text=review.text,
            media=[
                ReviewMediaResponse(
                    type=media.media_type,
                    url=media.url,
                    preview_url=media.preview_url,
                )
                for media in review.media
            ],
            business_reply=(
                BusinessReplyResponse(
                    text=review.business_reply_text,
                    published_at=review.business_reply_at,
                )
                if review.business_reply_text
                else None
            ),
        )
        for review in reviews
    ]
    return ReviewPage(page=page, page_size=page_size, total=total, items=items)


@router.get("/organizations/{organization_id}/reviews", response_model=ReviewPage)
def list_organization_reviews(
    organization_id: str,
    provider: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    project: Project = Depends(current_project),
    session: Session = Depends(get_session),
) -> ReviewPage:
    return list_reviews(
        organization_id=organization_id,
        provider=provider,
        page=page,
        page_size=page_size,
        project=project,
        session=session,
    )
