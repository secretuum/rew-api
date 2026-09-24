from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from rew_api.replies import extract_business_reply
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


class TwoGisProvider(ReviewProvider):
    code = "2gis"
    api_endpoint = "https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"
    public_api_host = "public-api.reviews.2gis.com"

    def resolve_source(self, url: str) -> ResolvedSource:
        original = url.strip()
        parsed = urlsplit(original)
        host = (parsed.hostname or "").lower()
        allowed_host = (
            host == "2gis.ru" or host.startswith("2gis.") or host.startswith("www.2gis.")
        )
        if parsed.scheme != "https" or not allowed_host:
            raise ProviderError("Expected an HTTPS 2GIS organization URL")

        path_parts = [part for part in parsed.path.split("/") if part]
        branch_id: str | None = None
        marker_index: int | None = None
        for marker in ("firm", "branch"):
            if marker in path_parts:
                marker_index = path_parts.index(marker)
                if marker_index + 1 < len(path_parts):
                    branch_id = path_parts[marker_index + 1]
                break

        if not branch_id or not branch_id.replace("_", "").replace("-", "").isalnum():
            raise ProviderError("Cannot extract 2GIS branch id from URL")

        # Keep the city segment: unlike the API, the public web page uses it to
        # select the correct regional frontend without an extra redirect.
        prefix = path_parts[:marker_index] if marker_index is not None else []
        normalized_path = "/" + "/".join((*prefix, "firm", branch_id, "tab", "reviews"))
        normalized_url = urlunsplit(("https", host, normalized_path, "", ""))
        return ResolvedSource(
            provider=self.code,
            source_url=original,
            normalized_url=normalized_url,
            external_org_id=branch_id,
        )

    def fetch_reviews(
        self,
        source: ResolvedSource,
        *,
        since: datetime | None = None,
    ) -> ProviderFetchResult:
        mode = self.settings.twogis_fetch_mode
        if mode == "api":
            return self._fetch_reviews_with_api(source, since=since)
        if mode == "auto" and self.settings.twogis_reviews_api_key:
            try:
                return self._fetch_reviews_with_api(source, since=since)
            except ProviderError:
                # An expired or unavailable official key must not stop a sync
                # when the public browser transport is enabled.
                pass
        return self._fetch_reviews_with_browser(source)

    def _fetch_reviews_with_browser(self, source: ResolvedSource) -> ProviderFetchResult:
        timeout_ms = int(self.settings.twogis_browser_timeout_seconds * 1_000)
        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.settings.twogis_browser_headless,
                    args=["--disable-dev-shm-usage"],
                )
                context = browser.new_context(
                    locale="ru-RU",
                    service_workers="block",
                    extra_http_headers={
                        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                    },
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)

                navigation = page.goto(
                    source.normalized_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if navigation is not None and not navigation.ok:
                    raise ProviderError(f"2GIS web page returned HTTP {navigation.status}")
                if urlsplit(page.url).hostname == "captcha.2gis.ru":
                    raise ProviderError("2GIS returned CAPTCHA instead of reviews")

                initial_payload = page.evaluate(
                    """
                    branchId => {
                        const queries = window.__REACT_QUERY_STATE__?.queries || [];
                        const query = queries.find(item => {
                            const key = item?.queryKey;
                            const params = key?.[1];
                            return key?.[0] === 'fetchEntityReviews'
                                && Array.isArray(params)
                                && String(params[0]) === String(branchId);
                        });
                        const first = query?.state?.data?.pages?.[0];
                        if (!first || !Array.isArray(first.items)) return null;
                        return {
                            meta: {
                                branch_rating: first.rating,
                                branch_reviews_count: first.total,
                                total_count: first.totalForRequest ?? first.total,
                            },
                            reviews: first.items,
                            next_link: first.next_link || null,
                        };
                    }
                    """,
                    source.external_org_id,
                )
                if not isinstance(initial_payload, dict):
                    raise ProviderError("2GIS web page does not contain embedded reviews")

                next_link = initial_payload.pop("next_link", None)
                if isinstance(next_link, str) and self._is_reviews_url(
                    next_link, source.external_org_id
                ):
                    first_url = self._replace_query_value(next_link, "offset", "0")
                else:
                    # This URL is never requested when the embedded first page
                    # already contains all reviews. It only supplies pagination
                    # defaults to the collector without inventing a web key.
                    page_size = max(len(initial_payload.get("reviews", [])), 1)
                    first_url = (
                        f"https://{self.public_api_host}/3.0/branches/"
                        f"{source.external_org_id}/reviews?limit={page_size}&offset=0"
                    )

                return self._collect_browser_pages(
                    context,
                    source,
                    first_url=first_url,
                    first_payload=initial_payload,
                    timeout_ms=timeout_ms,
                )
        except PlaywrightTimeoutError as exc:
            raise ProviderError(
                "2GIS did not load reviews in Chromium; the page may be blocked or changed"
            ) from exc
        except PlaywrightError as exc:
            # Playwright errors can contain the intercepted URL (including the
            # temporary web key), so do not copy their raw text into sync logs.
            raise ProviderError("Cannot collect 2GIS reviews with Chromium") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except PlaywrightError:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except PlaywrightError:
                    pass

    def _collect_browser_pages(
        self,
        context: BrowserContext,
        source: ResolvedSource,
        *,
        first_url: str,
        first_payload: dict[str, Any],
        timeout_ms: int,
    ) -> ProviderFetchResult:
        first_meta, first_page = self._parse_response_payload(first_payload)
        metrics = self._extract_metrics(first_meta)
        total_count = self._as_int(first_meta.get("total_count"))
        if total_count is None:
            total_count = self._as_int(first_meta.get("branch_reviews_count"))

        query = dict(parse_qsl(urlsplit(first_url).query, keep_blank_values=True))
        limit = self._as_int(query.get("limit")) or max(len(first_page), 1)
        offset = self._as_int(query.get("offset")) or 0
        collected: dict[str, ProviderReview] = {}
        is_complete = True
        current_page = first_page

        while True:
            for item in current_page:
                if not isinstance(item, dict):
                    continue
                review = self.parse_review(item)
                collected[review.external_id] = review
                if len(collected) >= self.settings.max_reviews_per_source:
                    is_complete = total_count is not None and len(collected) >= total_count
                    break

            if len(collected) >= self.settings.max_reviews_per_source:
                break

            next_offset = offset + limit
            if total_count is not None and next_offset >= total_count:
                break
            if not current_page or (total_count is None and len(current_page) < limit):
                break

            if self.settings.provider_request_delay_seconds:
                time.sleep(self.settings.provider_request_delay_seconds)

            next_url = self._replace_query_value(first_url, "offset", str(next_offset))
            api_response = context.request.get(
                next_url,
                headers={
                    "Accept": "application/json",
                    "Origin": f"{urlsplit(source.normalized_url).scheme}://"
                    f"{urlsplit(source.normalized_url).netloc}",
                    "Referer": source.normalized_url,
                },
                timeout=timeout_ms,
            )
            try:
                if not api_response.ok:
                    raise ProviderError(
                        f"2GIS web pagination returned HTTP {api_response.status}"
                    )
                payload = api_response.json()
            except PlaywrightError as exc:
                raise ProviderError("2GIS web pagination returned invalid JSON") from exc
            finally:
                api_response.dispose()

            if not isinstance(payload, dict):
                raise ProviderError("2GIS web pagination returned an unexpected response")
            _, current_page = self._parse_response_payload(payload)
            offset = next_offset

        return ProviderFetchResult(
            reviews=tuple(collected.values()),
            metrics=metrics,
            is_complete=is_complete,
        )

    def _fetch_reviews_with_api(
        self,
        source: ResolvedSource,
        *,
        since: datetime | None,
    ) -> ProviderFetchResult:
        api_key = self.settings.twogis_reviews_api_key
        if not api_key:
            raise ProviderError("REW_TWOGIS_REVIEWS_API_KEY is not configured")

        cutoff = None
        if since is not None:
            normalized_since = parse_datetime(since)
            if normalized_since is not None:
                cutoff = normalized_since - timedelta(minutes=self.settings.sync_overlap_minutes)

        params: dict[str, Any] = {
            "limit": 50,
            "offset_date": datetime.now(timezone.utc).isoformat(),
            "rated": "true",
            "sort_by": "date_edited",
            "key": api_key,
            "locale": "ru_RU",
            "fields": (
                "meta.branch_rating,meta.branch_reviews_count,meta.total_count,"
                "reviews.hiding_reason,reviews.is_verified,reviews.emojis"
            ),
        }
        endpoint = self.api_endpoint.format(branch_id=source.external_org_id)
        collected: dict[str, ProviderReview] = {}
        metrics: dict[str, Any] = {}
        last_cursor: str | None = None
        is_complete = True

        headers = {
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "User-Agent": "Mozilla/5.0 (compatible; ReviewsCollector/0.1)",
        }
        with httpx.Client(timeout=self.settings.http_timeout_seconds, headers=headers) as client:
            while len(collected) < self.settings.max_reviews_per_source:
                response = self.request(client, "GET", endpoint, params=params)
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderError("2GIS returned invalid JSON") from exc

                if not isinstance(payload, dict):
                    raise ProviderError("2GIS returned an unexpected response")

                meta, page = self._parse_response_payload(payload)
                if not metrics:
                    metrics = self._extract_metrics(meta)
                if not page:
                    break

                page_dates: list[datetime] = []
                for item in page:
                    if not isinstance(item, dict):
                        continue
                    review = self.parse_review(item)
                    collected[review.external_id] = review
                    relevant_date = review.edited_at or review.published_at
                    if relevant_date:
                        page_dates.append(relevant_date)
                    if len(collected) >= self.settings.max_reviews_per_source:
                        is_complete = False
                        break

                if cutoff and page_dates and min(page_dates) <= cutoff:
                    break

                last_item = page[-1] if isinstance(page[-1], dict) else {}
                cursor = last_item.get("date_created") or last_item.get("date_edited")
                if not isinstance(cursor, str) or not cursor or cursor == last_cursor:
                    break
                last_cursor = cursor
                params["offset_date"] = cursor
                if self.settings.provider_request_delay_seconds:
                    time.sleep(self.settings.provider_request_delay_seconds)

        return ProviderFetchResult(
            reviews=tuple(collected.values()),
            metrics=metrics,
            is_complete=is_complete,
        )

    def parse_review(self, payload: dict[str, Any]) -> ProviderReview:
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        avatar = first_http_url(user.get("photo_preview_urls"))
        published_at = parse_datetime(payload.get("date_created"))
        edited_at = parse_datetime(payload.get("date_edited"))
        text = str(payload.get("text") or "")
        external_id = str(payload.get("id") or "")
        if not external_id:
            external_id = stable_review_id(
                self.code,
                {
                    "author": user.get("id") or user.get("name"),
                    "published_at": published_at,
                    "text": text,
                },
            )

        media = self._extract_media(payload)
        try:
            rating = int(float(payload.get("rating") or 0))
        except (TypeError, ValueError):
            rating = 0

        reply = extract_business_reply("2gis", payload)
        return ProviderReview(
            external_id=external_id,
            author_name=str(user.get("name") or "Anonymous"),
            author_avatar_url=avatar,
            published_at=published_at,
            edited_at=edited_at,
            rating=max(0, min(5, rating)),
            text=text,
            media=media,
            raw_payload=payload,            business_reply_text=reply.text if reply else None,
            business_reply_at=reply.published_at if reply else None,
        )

    def _extract_media(self, payload: dict[str, Any]) -> tuple[ProviderMedia, ...]:
        result: list[ProviderMedia] = []
        seen: set[str] = set()

        groups = (
            ("photo", payload.get("photos")),
            ("video", payload.get("videos")),
            ("photo", payload.get("media")),
        )
        for default_type, values in groups:
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str):
                    url = value if value.startswith(("https://", "http://")) else None
                    item: dict[str, Any] = {}
                    item_type = default_type
                elif isinstance(value, dict):
                    item = value
                    item_type = default_type
                    for nested_type in ("photo", "video"):
                        nested = value.get(nested_type)
                        if isinstance(nested, dict):
                            item = nested
                            item_type = nested_type
                            break
                    url = first_http_url(
                        item.get("video_url")
                        or item.get("file_url")
                        or item.get("url")
                        or item.get("preview_urls")
                    )
                else:
                    continue
                if not url or url in seen:
                    continue

                raw_type = str(item.get("type") or item_type).lower()
                media_type = "video" if "video" in raw_type else item_type
                previews = item.get("preview_urls")
                preview_candidate: Any = None
                if isinstance(previews, dict):
                    preview_candidate = (
                        previews.get("640x")
                        or previews.get("320x")
                        or previews.get("64x64")
                    )
                preview = first_http_url(
                    item.get("preview_url")
                    or item.get("thumbnail_url")
                    or preview_candidate
                    or previews
                )
                result.append(
                    ProviderMedia(
                        media_type=media_type,
                        url=url,
                        preview_url=preview if preview != url else None,
                        external_id=str(item.get("id")) if item.get("id") else None,
                    )
                )
                seen.add(url)

        return tuple(result)

    @classmethod
    def _is_reviews_url(cls, url: str, branch_id: str) -> bool:
        parsed = urlsplit(url)
        if parsed.hostname != cls.public_api_host:
            return False
        parts = [part for part in parsed.path.split("/") if part]
        return (
            len(parts) == 4
            and parts[0] in {"2.0", "3.0"}
            and parts[1] == "branches"
            and parts[2] == branch_id
            and parts[3] == "reviews"
        )

    @staticmethod
    def _parse_response_payload(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[Any]]:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        reviews = payload.get("reviews")
        if not isinstance(reviews, list):
            raise ProviderError("2GIS returned an unexpected review list")
        return meta, reviews

    @staticmethod
    def _extract_metrics(meta: dict[str, Any]) -> dict[str, Any]:
        values = {
            "rating": meta.get("branch_rating"),
            "review_count": meta.get("branch_reviews_count") or meta.get("total_count"),
        }
        return {key: value for key, value in values.items() if value is not None}

    @staticmethod
    def _replace_query_value(url: str, key: str, value: str) -> str:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query[key] = value
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
