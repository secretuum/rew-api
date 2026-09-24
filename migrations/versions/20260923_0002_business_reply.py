"""Business reply of the organization to a review.

Revision ID: 20260923_0002
Revises: 20260717_0001
Create Date: 2026-09-23

Adds reviews.business_reply_text / business_reply_at and fills them for already stored reviews
from raw_payload (the reply is part of the provider payload), so no re-fetch is needed.
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260923_0002"
down_revision: str | None = "20260717_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("business_reply_text", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("business_reply_at", sa.DateTime(timezone=True), nullable=True)
        )

    from rew_api.replies import backfill_business_replies

    backfill_business_replies(op.get_bind())


def downgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.drop_column("business_reply_at")
        batch.drop_column("business_reply_text")
