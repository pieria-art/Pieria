"""artwork attribution (CC BY 4.0 credit fields)

Revision ID: 0007_artwork_attribution
Revises: 0006_artwork_aspect_crops
Create Date: 2026-09-27

Adds five additive, nullable columns to `artworks` (INFRA spec_ccby_attribution.md Stage B; ADR-142):

  * `license`          — a `core/licensing.py` PACK_ALLOWED id (e.g. "CC-BY-4.0"), not free text.
  * `license_url`      — the licence deed URL (from `core.licensing.LICENSE_URLS`).
  * `attribution`      — the credit line to display exactly as given (CC BY works only; PD/CC0 may
                          carry a courtesy credit).
  * `attribution_url`  — the source page URL for the attribution (may differ from `origin_url`).
  * `origin_url`       — the real web source of the work (fixes the pre-existing `/art/{id}` "View
                          original source" link, which today falls back to a broken `pack:…`
                          placeholder for pack installs).

NULL means "not known" (manual uploads, and installs made before this migration ran). No backfill —
existing installs get the data on the next pack re-download (install never backfills, ADR-135/F8).
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_artwork_attribution"
down_revision: str | Sequence[str] | None = "0006_artwork_aspect_crops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("license", "license_url", "attribution", "attribution_url", "origin_url")


def upgrade() -> None:
    # Idempotent (defensive-migration pattern, ADR-035): skip a column a create_all-built DB already has.
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("artworks")}
    with op.batch_alter_table("artworks") as batch_op:
        for col in _COLUMNS:
            if col not in existing:
                batch_op.add_column(sa.Column(col, sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artworks") as batch_op:
        for col in _COLUMNS:
            batch_op.drop_column(col)
