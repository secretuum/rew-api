from __future__ import annotations

import httpx

from rew_api.config import Settings
from rew_api.providers import ProviderFetchResult
from rew_api.providers.twogis import TwoGisProvider
from rew_api.providers.yandex import YandexMapsProvider


def test_twogis_resolves_url_and_preserves_full_review(settings: Settings) -> None:
    provider = TwoGisProvider(settings)
    source = provider.resolve_source(
        "https://2gis.ru/moscow/firm/70000001012345678/55.7,37.6/tab/reviews"
    )
    assert source.external_org_id == "70000001012345678"
    assert source.normalized_url == (
        "https://2gis.ru/moscow/firm/70000001012345678/tab/reviews"
    )

    full_text = "Очень подробный отзыв. " * 100
    review = provider.parse_review(
        {
            "id": "review-1",
            "user": {"name": "Иван", "photo_preview_urls": {}},
            "date_created": "2026-07-15T10:30:00+03:00",
            "date_edited": "2026-07-16T11:00:00+03:00",
            "rating": 5,
            "text": full_text,
            "photos": [{"id": "photo-1", "preview_urls": {"1920x": "https://img.test/full.jpg"}}],
            "videos": [
                {
                    "id": "video-1",
                    "video_url": "https://video.test/file.mp4",
                    "thumbnail_url": "https://img.test/video.jpg",
                }
            ],
            "media": [
                {
                    "photo": {
                        "id": "photo-2",
                        "preview_urls": {
                            "url": "https://img.test/original.jpg",
                            "640x": "https://img.test/preview.jpg",
                        },
                    }
                }
            ],
        }
    )

    assert review.author_avatar_url is None
    assert review.text == full_text
    assert review.published_at.isoformat() == "2026-07-15T07:30:00+00:00"
    assert [(item.media_type, item.url) for item in review.media] == [
        ("photo", "https://img.test/full.jpg"),
        ("video", "https://video.test/file.mp4"),
        ("photo", "https://img.test/original.jpg"),
    ]
    assert review.media[2].preview_url == "https://img.test/preview.jpg"


def test_twogis_uses_browser_without_configured_key(monkeypatch) -> None:
    provider = TwoGisProvider(
        Settings(
            twogis_fetch_mode="browser",
            twogis_reviews_api_key=None,
        )
    )
    source = provider.resolve_source(
        "https://2gis.ru/moscow/firm/70000001012345678/tab/reviews"
    )
    expected = ProviderFetchResult(reviews=(), metrics={"review_count": 0})

    def fake_browser_fetch(resolved_source):
        assert resolved_source == source
        return expected

    monkeypatch.setattr(provider, "_fetch_reviews_with_browser", fake_browser_fetch)
    result = provider.fetch_reviews(source)

    assert result == expected


def test_twogis_browser_transport_paginates_captured_request(settings: Settings) -> None:
    provider = TwoGisProvider(settings)
    source = provider.resolve_source(
        "https://2gis.ru/moscow/firm/70000001012345678/tab/reviews"
    )
    first_url = (
        "https://public-api.reviews.2gis.com/3.0/branches/70000001012345678/reviews"
        "?limit=2&offset=0&sort_by=trust&key=temporary-web-key&locale=ru_RU"
    )
    first_payload = {
        "meta": {
            "branch_rating": 4.8,
            "branch_reviews_count": 3,
            "total_count": 3,
        },
        "reviews": [
            {"id": "r1", "user": {"name": "One"}, "rating": 5, "text": "First"},
            {"id": "r2", "user": {"name": "Two"}, "rating": 4, "text": "Second"},
        ],
    }
    second_payload = {
        "meta": {"total_count": 3},
        "reviews": [
            {"id": "r3", "user": {"name": "Three"}, "rating": 5, "text": "Third"}
        ],
    }

    class FakeResponse:
        ok = True
        status = 200

        def json(self):
            return second_payload

        def dispose(self):
            return None

    class FakeRequest:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            assert kwargs["headers"]["Referer"] == source.normalized_url
            return FakeResponse()

    class FakeContext:
        def __init__(self) -> None:
            self.request = FakeRequest()

    context = FakeContext()
    result = provider._collect_browser_pages(
        context,  # type: ignore[arg-type]
        source,
        first_url=first_url,
        first_payload=first_payload,
        timeout_ms=10_000,
    )

    assert [review.external_id for review in result.reviews] == ["r1", "r2", "r3"]
    assert result.metrics == {"rating": 4.8, "review_count": 3}
    assert result.is_complete is True
    assert len(context.request.calls) == 1
    assert "offset=2" in context.request.calls[0]
    assert "key=temporary-web-key" in context.request.calls[0]


def test_twogis_only_intercepts_reviews_for_expected_branch() -> None:
    expected = (
        "https://public-api.reviews.2gis.com/3.0/branches/70000001012345678/reviews"
        "?limit=50&key=temporary"
    )
    other = expected.replace("70000001012345678", "70000001099999999")

    assert TwoGisProvider._is_reviews_url(expected, "70000001012345678") is True
    assert TwoGisProvider._is_reviews_url(other, "70000001012345678") is False


def test_yandex_maps_review_mapping_includes_avatar_and_media(settings: Settings) -> None:
    provider = YandexMapsProvider(settings)
    source = provider.resolve_source("https://yandex.ru/maps/org/example/191403044676/")
    assert source.external_org_id == "191403044676"

    review = provider.parse_review(
        {
            "reviewId": "ya-review-1",
            "author": {
                "name": "Анна",
                "avatarUrl": "https://avatars.mds.yandex.net/get-yapic/123/{size}",
            },
            "createdTime": "2026-07-10T11:00:00Z",
            "updatedTime": "2026-07-11T12:00:00Z",
            "rating": 4,
            "text": "Полный текст отзыва",
            "photos": [
                {
                    "photoId": "p1",
                    "urlTemplate": "https://avatars.mds.yandex.net/get-altay/1/{size}",
                }
            ],
            "videos": [
                {
                    "videoId": "v1",
                    "videoUrl": "https://video.test/review.mp4",
                    "thumbnailUrl": "https://img.test/preview.jpg",
                }
            ],
        }
    )

    assert review.author_avatar_url.endswith("/islands-200")
    assert review.text == "Полный текст отзыва"
    assert review.media[0].url.endswith("/XXXL")
    assert review.media[1].media_type == "video"


def test_yandex_fetches_internal_pages_from_context(settings: Settings, monkeypatch) -> None:
    provider = YandexMapsProvider(settings)
    source = provider.resolve_source("https://yandex.ru/maps/org/example/191403044676/")
    html = """
        <html><script>
        {"csrfToken":"csrf","sessionId":"session","requestId":"addrs-upper-42"}
        </script></html>
    """
    response_payload = {
        "data": {
            "reviews": [
                {
                    "reviewId": "r1",
                    "author": {"name": "User", "avatarUrl": ""},
                    "rating": 5,
                    "text": "Review",
                    "updatedTime": "2026-07-10T10:00:00Z",
                }
            ],
            "reviewsParams": {"count": 1, "page": 1, "totalPages": 1},
        }
    }

    calls: list[str] = []

    def fake_request(client, method, url, **kwargs):
        calls.append(url)
        request = httpx.Request(method, url)
        if "fetchReviews" in url:
            return httpx.Response(200, json=response_payload, request=request)
        return httpx.Response(200, text=html, request=request)

    monkeypatch.setattr(provider, "request", fake_request)
    result = provider.fetch_reviews(source)

    assert len(result.reviews) == 1
    assert result.metrics == {"review_count": 1}
    assert any("fetchReviews" in call and "s=" in call for call in calls)


def test_yandex_query_signature_is_order_independent(settings: Settings) -> None:
    provider = YandexMapsProvider(settings)
    first = provider._sign_query({"page": 1, "businessId": "42", "locale": "ru_RU"})
    second = provider._sign_query({"locale": "ru_RU", "businessId": "42", "page": 1})
    assert first == second


def test_yandex_keeps_regional_host_kz(settings: Settings) -> None:
    provider = YandexMapsProvider(settings)
    source = provider.resolve_source("https://yandex.kz/maps/org/del_cappuccino/100629485422/")
    assert source.external_org_id == "100629485422"
    assert source.normalized_url == "https://yandex.kz/maps/org/org/100629485422/reviews/"
    context = {
        "businessId": "100629485422",
        "csrfToken": "csrf",
        "sessionId": "session",
        "reqId": "addrs-upper-1",
        "retpath": source.normalized_url,
    }
    assert provider._build_api_url(context, page=1, page_size=50).startswith(
        "https://yandex.kz/maps/api/business/fetchReviews?"
    )
    ru = provider.resolve_source("https://www.yandex.ru/maps/org/example/191403044676/")
    assert ru.normalized_url == "https://yandex.ru/maps/org/org/191403044676/reviews/"


def test_yandex_follows_regional_redirect_once(settings: Settings) -> None:
    provider = YandexMapsProvider(settings)

    class _Resp:
        def __init__(self, status: int, location: str | None = None) -> None:
            self.status_code = status
            self.is_redirect = status in (301, 302, 307, 308)
            self.headers = {"location": location} if location else {}

    class _Client:
        def __init__(self, resp: _Resp) -> None:
            self.resp = resp

        def get(self, url: str) -> _Resp:
            return self.resp

    ru = "https://yandex.ru/maps/org/org/100629485422/reviews/"
    kz = "https://yandex.kz/maps/org/org/100629485422/reviews/"
    assert provider._regional_page_url(_Client(_Resp(302, kz)), ru) == kz
    assert provider._regional_page_url(_Client(_Resp(200)), ru) == ru
    assert provider._regional_page_url(_Client(_Resp(302, "https://evil.test/x")), ru) == ru
