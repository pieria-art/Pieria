"""L5: subscriptions.pinned_public_key / key_status / pending_public_key

Revision ID: 0011_subscription_key_pinning
Revises: 0010_pack_metadata_refreshed_at
Create Date: 2026-10-04

ADR-148 L5. Additive: pin a community publisher's signing key across federation syncs. Existing rows
get key_status='ok' and no pin (they pin on their next signed sync). Idempotent (ADR-035 pattern).
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_subscription_key_pinning"
down_revision: str | Sequence[str] | None = "0010_pack_metadata_refreshed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("subscriptions")}
    with op.batch_alter_table("subscriptions") as batch_op:
        if "pinned_public_key" not in have:
            batch_op.add_column(sa.Column("pinned_public_key", sa.String(), nullable=True))
        if "key_status" not in have:
            batch_op.add_column(sa.Column("key_status", sa.String(), nullable=False, server_default="ok"))
        if "pending_public_key" not in have:
            batch_op.add_column(sa.Column("pending_public_key", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("pending_public_key")
        batch_op.drop_column("key_status")
        batch_op.drop_column("pinned_public_key")
