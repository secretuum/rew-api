from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
from typing import Any

import httpx

from rew_api.config import Settings


class ProviderError(RuntimeError):
    """A provider URL or response cannot be processed safely."""


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    provider: str
    source_url: str
    normalized_url: str
    external_org_id: str


@dataclass(frozen=True, slots=True)
class ProviderMedia:
    media_type: str
    url: str
    preview_url: str | None = None
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderReview:
    external_id: str
    author_name: str
    author_avatar_url: str | None
    published_at: datetime | None
    edited_at: datetime | None
    rating: int
    text: str
    media: tuple[ProviderMedia, ...] = ()
    raw_payload: dict[str, Any] | None = None
    business_reply_text: str | None = None
    business_reply_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderFetchResult:
    reviews: tuple[ProviderReview, ...]
    metrics: dict[str, Any] = field(default_factory=dict)
    is_complete: bool = True


class ReviewProvider(ABC):
    code: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def resolve_source(self, url: str) -> ResolvedSource:
        raise NotImplementedError

    @abstractmethod
    def fetch_reviews(
        self,
        source: ResolvedSource,
        *,
        since: datetime | None = None,
    ) -> ProviderFetchResult:
        raise NotImplementedError

    def request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        last_error: Exception | None = None
        attempts = self.settings.http_retry_attempts

        for attempt in range(1, attempts + 1):
            try:
                response = client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ProviderError(
                        f"{self.code} temporarily returned HTTP {response.status_code}"
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ProviderError) as exc:
                last_error = exc
                if attempt == attempts:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))

        raise ProviderError(f"{self.code} request failed: {last_error}") from last_error


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime_time.min)
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for format_string in ("%Y-%m-%d", "%d.%m.%Y"):
                try:
                    parsed = datetime.strptime(normalized, format_string)
                    break
                except ValueError:
                    continue
            else:
                return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def first_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    if isinstance(value, dict):
        preferred = (
            "original",
            "origUrl",
            "originalUrl",
            "videoUrl",
            "fileUrl",
            "photoUrl",
            "imageUrl",
            "url",
            "href",
            "src",
            "1920x",
            "1280x",
            "640x",
            "320x",
        )
        for key in preferred:
            result = first_http_url(value.get(key))
            if result:
                return result
        for child in value.values():
            result = first_http_url(child)
            if result:
                return result
    if isinstance(value, list):
        for child in reversed(value):
            result = first_http_url(child)
            if result:
                return result
    return None


def stable_review_id(provider: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{provider}:{canonical}".encode()).hexdigest()
