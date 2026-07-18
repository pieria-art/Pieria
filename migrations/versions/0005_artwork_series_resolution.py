"""artwork series + resolution_tier (owned-art metadata)

Revision ID: 0005_artwork_series_resolution
Revises: 0004_gallery_source_collection
Create Date: 2026-07-18

Adds two additive, nullable columns to `artworks`, both carried through the pack manifest into owned art:

  * `series`          — the series/set a print belongs to (e.g. a ukiyo-e "Famous Places in the
                        Eastern Capital"), lifted from the source title by tools/clean_titles.py.
                        Rendered as a placard subtitle. NULL for works with no series.
  * `resolution_tier` — the honest "HD"|"4K"|"8K" quality tag authored offline by
                        tools/tag_resolution.py (resolution-tags spec §6). Surfaces delivered
                        quality on owned art via a passive badge. NULL until re-seed from a pack
                        republish that carries the field.

Both are metadata-only: no index, no backfill (values arrive on the next pack re-seed).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_artwork_series_resolution"
down_revision: Union[str, Sequence[str], None] = "0004_gallery_source_collection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent (defensive-migration pattern, ADR-035): skip a column a create_all-built DB already has.
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("artworks")}
    with op.batch_alter_table("artworks") as batch_op:
        if "series" not in existing:
            batch_op.add_column(sa.Column("series", sa.String(), nullable=True))
        if "resolution_tier" not in existing:
            batch_op.add_column(sa.Column("resolution_tier", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artworks") as batch_op:
        batch_op.drop_column("resolution_tier")
        batch_op.drop_column("series")
