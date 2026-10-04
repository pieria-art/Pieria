"""F8b: subscriptions.metadata_refreshed_at

Revision ID: 0010_pack_metadata_refreshed_at
Revises: 0009_pack_refresh_tracking
Create Date: 2026-10-04

ADR-148 F8b. Additive, nullable: when a metadata-only manifest refresh last ran for the pack, so the
packs card can say "metadata updated <date>". Idempotent (defensive-migration pattern, ADR-035).
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_pack_metadata_refreshed_at"
down_revision: str | Sequence[str] | None = "0009_pack_refresh_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "metadata_refreshed_at" not in {c["name"] for c in insp.get_columns("subscriptions")}:
        with op.batch_alter_table("subscriptions") as batch_op:
            batch_op.add_column(sa.Column("metadata_refreshed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("metadata_refreshed_at")
