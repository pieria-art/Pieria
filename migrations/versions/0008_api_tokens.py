"""public API: api_tokens table + active_displays.kind

Revision ID: 0008_api_tokens
Revises: 0007_artwork_attribution
Create Date: 2026-10-04

Public API v1 foundation (ADR-147, spec_public_api_and_ha.md §2). Additive only:

  * `api_tokens` — bearer tokens for /api/v1. Only the sha256 of the token is stored (the plaintext is
    shown once at mint time); `scopes` is a comma-separated subset of {read, control}.
  * `active_displays.kind` — nullable 'canvas' | 'eink' marker written by the two paths that already
    own the row (the Canvas WS heartbeat, the e-ink pull). Lets the API report a display's kind
    without guessing; NULL = not seen since this migration (reported as 'unknown').

Idempotent (defensive-migration pattern, ADR-035): skips what a create_all-built DB already has.
(The spec called this 0002_api_tokens; the chain was already at 0007 when A1 was built.)
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_api_tokens"
down_revision: str | Sequence[str] | None = "0007_artwork_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "api_tokens" not in insp.get_table_names():
        op.create_table(
            "api_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(), nullable=False),
            sa.Column("scopes", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_api_tokens_id", "api_tokens", ["id"])
        op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)
    existing = {c["name"] for c in insp.get_columns("active_displays")}
    if "kind" not in existing:
        with op.batch_alter_table("active_displays") as batch_op:
            batch_op.add_column(sa.Column("kind", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("active_displays") as batch_op:
        batch_op.drop_column("kind")
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_index("ix_api_tokens_id", table_name="api_tokens")
    op.drop_table("api_tokens")
