"""F8 freshness: subscriptions.applied_generated_at

Revision ID: 0012_pack_applied_generated_at
Revises: 0011_subscription_key_pinning
Create Date: 2026-10-05

The signed `generated_at` of the manifest last applied to a pack's works. A metadata refresh refuses a
fetched manifest older than this (rollback via a replayed, still-validly-signed manifest). NULL = legacy
(allow once). Additive, nullable, idempotent (ADR-035 pattern).
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_pack_applied_generated_at"
down_revision: str | Sequence[str] | None = "0011_subscription_key_pinning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "applied_generated_at" not in {c["name"] for c in insp.get_columns("subscriptions")}:
        with op.batch_alter_table("subscriptions") as batch_op:
            batch_op.add_column(sa.Column("applied_generated_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("applied_generated_at")
