from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import httpx

from rew_api.providers.base import (
    ProviderError,
    ProviderFetchResult,
    ProviderMedia,
    ProviderReview,
    ResolvedSource,
    ReviewProvider,
    first_http_url,
    parse_datetime,
    stable_review_id,
)


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_script = False
        self._current: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._inside_script = True
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._inside_script:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_script:
            self.scripts.append("".join(self._current))
            self._inside_script = False
            self._current = []


class YandexMapsProvider(ReviewProvider):
    code = "yandex"
    api_endpoint = "https://yandex.ru/maps/api/business/fetchReviews"

    _long_url_id = re.compile(r"/maps/org/(?:[^/?#]+/)?(\d+)(?:/|$)")

    def resolve_source(self, url: str) -> ResolvedSource:
        original = url.strip()
        self._validate_yandex_url(original)
        resolved = self._resolve_short_url(original) if "/maps/-/" in original else original
        external_id = self._extract_organization_id(resolved)
        if not external_id:
            raise ProviderError("Cannot extract Yandex organization id from URL")

        # Keep the regional host of the source URL (yandex.kz for Kazakhstan): yandex.ru answers
        # with a 302 to the regional domain there, and the client does not follow redirects.
        normalized = f"https://{self._canonical_host(resolved)}/maps/org/org/{external_id}/reviews/"
        return ResolvedSource(
            provider=self.code,
            source_url=original,
            normalized_url=normalized,
            external_org_id=external_id,
        )

    def fetch_reviews(
        self,
        source: ResolvedSource,
        *,
        since: datetime | None = None,
    ) -> ProviderFetchResult:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }
        reviews: dict[str, ProviderReview] = {}
        count: int | None = None
        is_complete = True
        cutoff = None
        if since:
            normalized_since = parse_datetime(since)
            if normalized_since is not None:
                cutoff = normalized_since - timedelta(minutes=self.settings.sync_overlap_minutes)

        with httpx.Client(
            timeout=self.settings.http_timeout_seconds,
            headers=headers,
            follow_redirects=False,
        ) as client:
            page_response = self.request(client, "GET", source.normalized_url)
            body = page_response.text
            if self._looks_like_bot_protection(body):
                raise ProviderError("Yandex returned bot protection instead of reviews")

            context = self._extract_context(body, source.external_org_id, source.normalized_url)
            embedded_payloads = self._extract_embedded_payloads(body)
            for payload in embedded_payloads:
                parsed = self.parse_review(payload)
                reviews[parsed.external_id] = parsed

            if context is None:
                if not reviews:
                    raise ProviderError(
                        "Yandex review context was not found; page format may have changed"
                    )
                return ProviderFetchResult(
                    reviews=tuple(reviews.values()),
                    metrics={},
                    is_complete=False,
                )

            page = 1
            total_pages: int | None = None
            while len(reviews) < self.settings.max_reviews_per_source:
                api_url = self._build_api_url(context, page=page, page_size=50)
                response = self.request(
                    client,
                    "GET",
                    api_url,
                    headers={
                        "Referer": source.normalized_url,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json",
                    },
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    if self._looks_like_bot_protection(response.text):
                        raise ProviderError("Yandex blocked the reviews request") from exc
                    raise ProviderError("Yandex returned invalid review JSON") from exc

                page_payloads = self._find_review_payloads(payload)
                params = self._find_page_params(payload)
                if params:
                    count = self._as_int(params.get("count"), default=count)
                    total_pages = self._as_int(params.get("totalPages"), default=total_pages)

                page_dates: list[datetime] = []
                for item in page_payloads:
                    parsed = self.parse_review(item)
                    reviews[parsed.external_id] = parsed
                    relevant_date = parsed.edited_at or parsed.published_at
                    if relevant_date:
                        page_dates.append(relevant_date)
                    if len(reviews) >= self.settings.max_reviews_per_source:
                        is_complete = False
                        break

                if not page_payloads:
                    break
                if cutoff and page_dates and min(page_dates) <= cutoff:
                    break
                if total_pages is not None and page >= total_pages:
                    break

                page += 1
                if page > 1_000:
                    is_complete = False
                    break
                if self.settings.provider_request_delay_seconds:
                    time.sleep(self.settings.provider_request_delay_seconds)

        metrics = {"review_count": count} if count is not None else {}
        return ProviderFetchResult(
            reviews=tuple(reviews.values()),
            metrics=metrics,
            is_complete=is_complete,
        )

    def parse_review(self, payload: dict[str, Any]) -> ProviderReview:
        author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
        text = str(payload.get("text") or payload.get("description") or "")
        published_at = parse_datetime(
            payload.get("createdTime") or payload.get("publishTime") or payload.get("datePublished")
        )
        edited_at = parse_datetime(payload.get("updatedTime") or payload.get("editedTime"))
        external_id = str(payload.get("reviewId") or payload.get("id") or "")
        if not external_id:
            external_id = stable_review_id(
                self.code,
                {
                    "author": author.get("id") or author.get("name"),
                    "published_at": published_at,
                    "text": text,
                },
            )

        avatar = first_http_url(author.get("avatarUrl") or author.get("avatar"))
        if avatar:
            avatar = avatar.replace("{size}", "islands-200").replace("%s", "islands-200")

        try:
            rating = int(float(payload.get("rating") or 0))
        except (TypeError, ValueError):
            rating = 0

        return ProviderReview(
            external_id=external_id,
            author_name=str(author.get("name") or "Anonymous"),
            author_avatar_url=avatar,
            published_at=published_at,
            edited_at=edited_at,
            rating=max(0, min(5, rating)),
            text=text,
            media=self._extract_media(payload),
            raw_payload=payload,
        )

    def _resolve_short_url(self, url: str) -> str:
        current = url
        with httpx.Client(
            timeout=self.settings.http_timeout_seconds, follow_redirects=False
        ) as client:
            for _ in range(5):
                self._validate_yandex_url(current)
                try:
                    response = client.get(current)
                except httpx.HTTPError as exc:
                    raise ProviderError(f"Cannot resolve Yandex short URL: {exc}") from exc
                if not response.is_redirect:
                    if response.status_code >= 400:
                        raise ProviderError(
                            f"Cannot resolve Yandex short URL: HTTP {response.status_code}"
                        )
                    return str(response.url)
                location = response.headers.get("location")
                if not location:
                    break
                current = urljoin(current, location)
        raise ProviderError("Yandex short URL has too many redirects")

    _hosts = ("yandex.ru", "yandex.com", "yandex.kz")

    @classmethod
    def _canonical_host(cls, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        return host if host in cls._hosts else cls._hosts[0]

    @classmethod
    def _validate_yandex_url(cls, url: str) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme != "https" or host not in cls._hosts or not parsed.path.startswith("/maps/"):
            raise ProviderError("Expected an HTTPS Yandex Maps URL")

    def _extract_organization_id(self, url: str) -> str | None:
        decoded = html.unescape(url)
        match = self._long_url_id.search(decoded)
        if match:
            return match.group(1)

        query = parse_qs(urlsplit(decoded).query)
        for key in ("oid", "org_id"):
            value = query.get(key, [None])[0]
            if value and str(value).isdigit():
                return str(value)

        oid_match = re.search(r"(?:oid|org_id)(?:%3D|=)(\d+)", decoded, re.IGNORECASE)
        return oid_match.group(1) if oid_match else None

    def _extract_context(self, body: str, business_id: str, retpath: str) -> dict[str, str] | None:
        csrf = self._extract_json_string(body, "csrfToken")
        session = self._extract_json_string(body, "sessionId")
        request_ids = self._extract_all_json_strings(body, "requestId")
        request_id = next((value for value in request_ids if "addrs-upper" in value), None)
        if request_id is None and request_ids:
            request_id = request_ids[-1]
        if not csrf or not session or not request_id:
            return None
        return {
            "businessId": business_id,
            "csrfToken": csrf,
            "sessionId": session,
            "reqId": request_id,
            "retpath": retpath,
        }

    def _build_api_url(self, context: dict[str, str], *, page: int, page_size: int) -> str:
        query: dict[str, Any] = {
            "ajax": 1,
            "businessId": context["businessId"],
            "csrfToken": context["csrfToken"],
            "locale": "ru_RU",
            "page": page,
            "pageSize": page_size,
            # Chronological order makes the overlap watermark safe for
            # incremental daily synchronization.
            "ranking": "by_time",
            "reqId": context["reqId"],
            "sessionId": context["sessionId"],
        }
        query["s"] = self._sign_query(query)
        # The API lives on the same regional host as the reviews page (retpath).
        endpoint = self.api_endpoint.replace(
            "yandex.ru", self._canonical_host(context.get("retpath", "")), 1
        )
        return f"{endpoint}?{urlencode(query, quote_via=quote)}"

    @staticmethod
    def _sign_query(query: dict[str, Any]) -> str:
        ordered = sorted(query.items(), key=lambda item: item[0].lower())
        query_string = urlencode(ordered, quote_via=quote)
        result = 5381
        for byte in query_string.encode("utf-8"):
            result = ((33 * result) ^ byte) & 0xFFFFFFFF
        return str(result)

    @staticmethod
    def _extract_json_string(text: str, key: str) -> str | None:
        values = YandexMapsProvider._extract_all_json_strings(text, key)
        return values[0] if values else None

    @staticmethod
    def _extract_all_json_strings(text: str, key: str) -> list[str]:
        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"')
        values: list[str] = []
        for match in pattern.finditer(text):
            try:
                decoded = json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                decoded = match.group(1)
            if isinstance(decoded, str):
                values.append(decoded)
        return values

    def _extract_embedded_payloads(self, body: str) -> list[dict[str, Any]]:
        collector = _ScriptCollector()
        collector.feed(body)
        found: dict[str, dict[str, Any]] = {}

        for script in collector.scripts:
            candidate = html.unescape(script.strip())
            if not candidate:
                continue
            possible_json = [candidate]
            first_brace = min(
                (index for index in (candidate.find("{"), candidate.find("[")) if index >= 0),
                default=-1,
            )
            if first_brace > 0:
                possible_json.append(candidate[first_brace:].rstrip("; \r\n"))

            for value in possible_json:
                try:
                    decoded = json.loads(value)
                except (json.JSONDecodeError, RecursionError):
                    continue
                for payload in self._find_review_payloads(decoded):
                    key = str(payload.get("reviewId") or stable_review_id(self.code, payload))
                    found[key] = payload

        return list(found.values())

    def _find_review_payloads(self, payload: Any) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                has_id = "reviewId" in value or "id" in value
                if has_id and isinstance(value.get("author"), dict) and "rating" in value:
                    key = str(value.get("reviewId") or value.get("id"))
                    found[key] = value
                    return
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return list(found.values())

    @staticmethod
    def _find_page_params(payload: Any) -> dict[str, Any] | None:
        result: dict[str, Any] | None = None

        def walk(value: Any) -> None:
            nonlocal result
            if result is not None:
                return
            if isinstance(value, dict):
                if "count" in value and "page" in value and "totalPages" in value:
                    result = value
                    return
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return result

    def _extract_media(self, payload: dict[str, Any]) -> tuple[ProviderMedia, ...]:
        result: list[ProviderMedia] = []
        seen: set[str] = set()
        groups = (
            ("photo", payload.get("photos")),
            ("video", payload.get("videos")),
            ("photo", payload.get("media")),
            ("photo", payload.get("attachments")),
        )

        for default_type, values in groups:
            if not isinstance(values, list):
                continue
            for value in values:
                item = value if isinstance(value, dict) else {}
                url = first_http_url(value)
                if not url:
                    template = item.get("urlTemplate") or item.get("photoUrlTemplate")
                    if isinstance(template, str):
                        url = template.replace("{size}", "XXXL").replace("%s", "XXXL")
                if not url or url in seen:
                    continue
                url = url.replace("{size}", "XXXL").replace("%s", "XXXL")
                raw_type = str(item.get("type") or default_type).lower()
                media_type = "video" if "video" in raw_type else default_type
                preview = first_http_url(
                    item.get("previewUrl") or item.get("thumbnailUrl") or item.get("preview")
                )
                result.append(
                    ProviderMedia(
                        media_type=media_type,
                        url=url,
                        preview_url=preview if preview != url else None,
                        external_id=str(
                            item.get("id") or item.get("photoId") or item.get("videoId") or ""
                        )
                        or None,
                    )
                )
                seen.add(url)

        return tuple(result)

    @staticmethod
    def _looks_like_bot_protection(body: str) -> bool:
        lowered = body.lower()
        return any(
            marker in lowered
            for marker in ("showcaptcha", "smartcaptcha", "confirm that you are not a robot")
        )

    @staticmethod
    def _as_int(value: Any, *, default: int | None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
