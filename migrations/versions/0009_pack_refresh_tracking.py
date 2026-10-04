"""F8 pack metadata refresh: artworks.user_edited_fields + subscriptions.applied_manifest_hash

Revision ID: 0009_pack_refresh_tracking
Revises: 0008_api_tokens
Create Date: 2026-10-04

ADR-135 / ADR-148 (F8). Additive only:

  * `artworks.user_edited_fields` — JSON array of column names the USER edited (default '[]'); a pack
    refresh never overwrites a listed field.
  * `subscriptions.applied_manifest_hash` — sha256 of the manifest last applied to existing works;
    a differing hash on boot triggers a metadata refresh. NULL = refresh once.

Idempotent (defensive-migration pattern, ADR-035): skips columns a create_all-built DB already has.
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_pack_refresh_tracking"
down_revision: str | Sequence[str] | None = "0008_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(insp, table: str) -> set:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "user_edited_fields" not in _cols(insp, "artworks"):
        with op.batch_alter_table("artworks") as batch_op:
            batch_op.add_column(sa.Column("user_edited_fields", sa.Text(), nullable=False, server_default="[]"))
    if "applied_manifest_hash" not in _cols(insp, "subscriptions"):
        with op.batch_alter_table("subscriptions") as batch_op:
            batch_op.add_column(sa.Column("applied_manifest_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("applied_manifest_hash")
    with op.batch_alter_table("artworks") as batch_op:
        batch_op.drop_column("user_edited_fields")
