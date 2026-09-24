"""Business reply ("ответ заведения") extracted from provider payloads.

Both providers already keep the raw review payload (``Review.raw_payload``); the reply lives
there, so it is extracted by a pure function that is shared by the providers (new reviews) and
by the migration backfill (reviews stored before the field existed — no re-fetch needed).

* 2GIS public reviews API: ``official_answer: {text, date_created}``.
* Yandex Maps review state: ``businessComment`` (seen as ``officialComment`` / ``businessAnswer``
  in some page versions) with ``text`` and ``updatedTime`` / ``createdTime`` / ``time``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


_KEYS = {
    "2gis": ("official_answer", "officialAnswer"),
    "yandex": ("businessComment", "officialComment", "businessAnswer", "business_comment"),
}
_TIME_KEYS = ("date_created", "date_edited", "updatedTime", "createdTime", "time", "date")
_MAX_TEXT = 8_000


@dataclass(frozen=True, slots=True)
class BusinessReply:
    text: str
    published_at: datetime | None


def extract_business_reply(provider: str, payload: Any) -> BusinessReply | None:
    """Reply of the organization to a review, or None when there is none."""
    # Imported here: the providers import this module, and the 0002 migration imports it first
    # (a module-level import of rew_api.providers would be circular).
    from rew_api.providers.base import parse_datetime

    if not isinstance(payload, dict):
        return None
    for key in _KEYS.get(provider, ()):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return BusinessReply(text=value.strip()[:_MAX_TEXT], published_at=None)
        if not isinstance(value, dict):
            continue
        text = value.get("text") or value.get("comment") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        published = None
        for time_key in _TIME_KEYS:
            published = parse_datetime(value.get(time_key))
            if published is not None:
                break
        return BusinessReply(text=text.strip()[:_MAX_TEXT], published_at=published)
    return None


def backfill_business_replies(connection, batch_size: int = 500) -> int:
    """Fill business_reply_* of stored reviews from their raw payloads. Returns updated rows.

    Used by the 0002 migration; idempotent (rows that already have a reply are skipped)."""
    import sqlalchemy as sa

    reviews = sa.table(
        "reviews",
        sa.column("id", sa.Integer),
        sa.column("source_id", sa.Integer),
        sa.column("raw_payload", sa.JSON),
        sa.column("business_reply_text", sa.Text),
        sa.column("business_reply_at", sa.DateTime(timezone=True)),
    )
    sources = sa.table("organization_sources", sa.column("id", sa.Integer),
                       sa.column("provider", sa.String))
    query = (
        sa.select(reviews.c.id, reviews.c.raw_payload, sources.c.provider)
        .select_from(reviews.join(sources, reviews.c.source_id == sources.c.id))
        .where(reviews.c.business_reply_text.is_(None), reviews.c.raw_payload.is_not(None))
        .order_by(reviews.c.id)
    )
    updated = 0
    pending: list[dict[str, Any]] = []
    for row in connection.execute(query):
        reply = extract_business_reply(row.provider, row.raw_payload)
        if reply is None:
            continue
        pending.append({"rid": row.id, "text": reply.text, "at": reply.published_at})
        if len(pending) >= batch_size:
            updated += _flush(connection, reviews, pending)
    if pending:
        updated += _flush(connection, reviews, pending)
    return updated


def _flush(connection, reviews, pending: list[dict[str, Any]]) -> int:
    import sqlalchemy as sa

    connection.execute(
        reviews.update()
        .where(reviews.c.id == sa.bindparam("rid"))
        .values(business_reply_text=sa.bindparam("text"), business_reply_at=sa.bindparam("at")),
        pending,
    )
    count = len(pending)
    pending.clear()
    return count
