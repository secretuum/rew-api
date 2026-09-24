from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ApiKeyCreated(BaseModel):
    id: int
    name: str
    prefix: str
    api_key: str
    created_at: datetime


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)


class OrganizationSourceCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=10, max_length=4_000)
    sync_interval_minutes: int | None = Field(default=None, ge=5, le=525_600)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        aliases = {
            "2gis": "2gis",
            "2гис": "2gis",
            "dgis": "2gis",
            "yandex": "yandex",
            "yandex_maps": "yandex",
            "яндекс": "yandex",
        }
        normalized = value.strip().lower()
        return aliases.get(normalized, normalized)


class SourceResponse(BaseModel):
    id: int
    provider: str
    source_url: str
    external_org_id: str
    enabled: bool
    sync_status: str
    sync_error: str | None
    last_success_at: datetime | None
    next_sync_at: datetime | None
    metrics: dict | None


class OrganizationResponse(BaseModel):
    id: str
    name: str
    sources: list[SourceResponse]


class ReviewMediaResponse(BaseModel):
    type: str
    url: str
    preview_url: str | None


class BusinessReplyResponse(BaseModel):
    text: str
    published_at: datetime | None


class ReviewResponse(BaseModel):
    id: str
    organization_id: str
    provider: str
    author_name: str
    author_avatar_url: str | None
    published_at: datetime | None
    edited_at: datetime | None
    rating: int
    text: str
    media: list[ReviewMediaResponse]
    business_reply: BusinessReplyResponse | None = None


class ReviewPage(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[ReviewResponse]


class SyncSummaryResponse(BaseModel):
    source_id: int
    status: str
    fetched_count: int
    created_count: int
    updated_count: int
    error: str | None = None
