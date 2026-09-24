from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import select

from rew_api.config import Settings
from rew_api.models import Organization, OrganizationSource, Project, Review
from rew_api.providers.twogis import TwoGisProvider
from rew_api.providers.yandex import YandexMapsProvider
from rew_api.replies import backfill_business_replies, extract_business_reply
from rew_api.security import create_api_key
from rew_api.services.sync import ReviewSyncService

from tests.test_sync import FakeProvider, FakeRegistry


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_extract_reply_both_providers() -> None:
    reply = extract_business_reply(
        "2gis",
        {"official_answer": {"text": " Спасибо! ", "date_created": "2026-09-01T10:00:00+05:00"}},
    )
    assert reply.text == "Спасибо!"
    assert reply.published_at.isoformat() == "2026-09-01T05:00:00+00:00"
    reply = extract_business_reply(
        "yandex", {"businessComment": {"text": "Разберёмся", "updatedTime": "2026-09-02T08:00:00Z"}}
    )
    assert reply.text == "Разберёмся" and reply.published_at.day == 2
    assert extract_business_reply("yandex", {"officialComment": "Строкой"}).text == "Строкой"
    assert extract_business_reply("2gis", {"official_answer": None}) is None
    assert extract_business_reply("2gis", {"official_answer": {"text": "  "}}) is None
    assert extract_business_reply("yandex", {"official_answer": {"text": "не тот ключ"}}) is None
    assert extract_business_reply("vk", {"official_answer": {"text": "x"}}) is None
    assert extract_business_reply("2gis", None) is None


def test_providers_map_reply(settings: Settings) -> None:
    review = TwoGisProvider(settings).parse_review(
        {"id": "r1", "user": {"name": "Иван"}, "date_created": "2026-09-01T10:00:00Z",
         "rating": 2, "text": "Долго", "official_answer": {"text": "Извините",
                                                           "date_created": "2026-09-02T10:00:00Z"}}
    )
    assert review.business_reply_text == "Извините" and review.business_reply_at.day == 2
    review = YandexMapsProvider(settings).parse_review(
        {"reviewId": "y1", "author": {"name": "Анна"}, "createdTime": "2026-09-01T10:00:00Z",
         "rating": 5, "text": "Отлично"}
    )
    assert review.business_reply_text is None and review.business_reply_at is None


def test_sync_stores_reply_and_api_returns_it(client, settings, session_factory) -> None:
    provider = FakeProvider(settings)
    service = ReviewSyncService(settings, FakeRegistry(provider))
    base_fetch = provider.fetch_reviews

    def with_reply(source, *, since=None):
        result = base_fetch(source, since=since)
        review = replace(result.reviews[0], business_reply_text="Ответ",
                         business_reply_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
        return replace(result, reviews=(review,))

    with session_factory() as session:
        project = Project(name="P", slug="p")
        org = Organization(name="Org", project=project)
        source = OrganizationSource(organization=org, provider="fake", source_url="https://s.test/1",
                                    normalized_url="https://s.test/1", external_org_id="company-1")
        session.add(source)
        session.commit()
        _, key = create_api_key(session, project, name="t", settings=settings)
        session.commit()
        service.sync_source(session, source.id)                     # без ответа
        body = client.get("/v1/reviews", headers={"Authorization": f"Bearer {key}"}).json()
        assert body["items"][0]["business_reply"] is None
        provider.fetch_reviews = with_reply
        summary = service.sync_source(session, source.id)          # ответ появился — updated
        assert summary.updated_count == 1
        stored = session.scalar(select(Review).where(Review.source_id == source.id))
        assert stored.business_reply_text == "Ответ"
        body = client.get("/v1/reviews", headers={"Authorization": f"Bearer {key}"}).json()
        assert body["items"][0]["business_reply"]["text"] == "Ответ"
        assert body["items"][0]["business_reply"]["published_at"].startswith("2026-07-02")


def test_backfill_from_raw_payload_is_idempotent(settings, session_factory) -> None:
    with session_factory() as session:
        project = Project(name="P", slug="p")
        org = Organization(name="Org", project=project)
        src = OrganizationSource(organization=org, provider="2gis", source_url="https://2gis.kz/x",
                                 normalized_url="https://2gis.kz/x", external_org_id="7000")
        session.add_all([
            Review(organization=org, source=src, provider_review_id="a", text="t", rating=2,
                   raw_payload={"official_answer": {"text": "Спасибо",
                                                    "date_created": "2026-09-01T10:00:00Z"}}),
            Review(organization=org, source=src, provider_review_id="b", text="t", rating=5,
                   raw_payload={"id": "b"}),
            Review(organization=org, source=src, provider_review_id="c", text="t", rating=4,
                   raw_payload=None),
        ])
        session.commit()
        engine = session.get_bind()
    with engine.begin() as conn:
        assert backfill_business_replies(conn) == 1
    with engine.begin() as conn:
        assert backfill_business_replies(conn) == 0
    with session_factory() as session:
        got = {r.provider_review_id: r.business_reply_text for r in session.scalars(select(Review))}
    assert got == {"a": "Спасибо", "b": None, "c": None}


def test_alembic_upgrade_0001_to_0002_backfills(tmp_path) -> None:
    """Настоящий прогон миграций: база на 0001 с отзывом → upgrade head → ответ заполнен."""
    db = tmp_path / "mig.db"
    env = dict(os.environ, REW_DATABASE_URL=f"sqlite:///{db}", PYTHONPATH=os.path.join(ROOT, "src"))

    def alembic(*args):
        run = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stderr[-2000:]

    alembic("upgrade", "20260717_0001")
    engine = sa.create_engine(f"sqlite:///{db}")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO projects (id, public_id, name, slug, is_active, created_at, updated_at) "
            "VALUES (1, 'p1', 'P', 'p', 1, :t, :t)"), {"t": now})
        cols = {c["name"] for c in sa.inspect(conn).get_columns("organizations")}
        conn.execute(sa.text(
            "INSERT INTO organizations (" + ", ".join(sorted(cols)) + ") VALUES ("
            + ", ".join(":" + c for c in sorted(cols)) + ")"),
            {c: {"id": 1, "public_id": "o1", "project_id": 1, "name": "Org", "is_active": 1}.get(c, now)
             for c in cols})
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("organization_sources")}
        vals = {"id": 1, "organization_id": 1, "provider": "2gis", "source_url": "https://2gis.kz/x",
                "normalized_url": "https://2gis.kz/x", "external_org_id": "7000", "enabled": 1,
                "sync_status": "success", "sync_interval_minutes": 60, "failure_count": 0}
        row = {c: vals.get(c, now if not cols[c]["nullable"] else None) for c in cols}
        conn.execute(sa.text("INSERT INTO organization_sources (" + ", ".join(row) + ") VALUES ("
                             + ", ".join(":" + c for c in row) + ")"), row)
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("reviews")}
        vals = {"id": 1, "organization_id": 1, "source_id": 1, "provider_review_id": "a",
                "author_name": "A", "rating": 2, "text": "t", "is_visible": 1,
                "raw_payload": '{"official_answer": {"text": "Спасибо"}}'}
        row = {c: vals.get(c, now if not cols[c]["nullable"] else None) for c in cols}
        conn.execute(sa.text("INSERT INTO reviews (" + ", ".join(row) + ") VALUES ("
                             + ", ".join(":" + c for c in row) + ")"), row)
    alembic("upgrade", "head")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT business_reply_text FROM reviews")).scalar() == "Спасибо"
    alembic("downgrade", "20260717_0001")
    with engine.connect() as conn:
        assert "business_reply_text" not in {
            c["name"] for c in sa.inspect(conn).get_columns("reviews")}
