"""artwork aspect_crops (per-shape render-time crop boxes)

Revision ID: 0006_artwork_aspect_crops
Revises: 0005_artwork_series_resolution
Create Date: 2026-07-20

Adds one additive, nullable column to `artworks`:

  * `aspect_crops_json` — up to four normalized crop boxes ({"16:9":[x0,y0,x1,y1], "9:16":[...],
                          "4:3":[...], "3:4":[...]}, floats 0..1), authored per work to answer "how
                          do I best fill THIS screen shape with this work" at render time. Stored as
                          JSON text (repo convention, see `unplayed_artworks_json`/`items_json`).
                          NULL means "not derived" — the renderer falls back to the focal cover
                          (`epaper.pick_crop_for_aspect` / callers already treat missing crops as
                          such). Not to be confused with the Tier-1 `crop_box`/`needs_frame_crop`
                          (baked into the master at pack build) or the LCD-only
                          `crop_x/crop_y/crop_width/crop_height` original-pixel rect.

Metadata-only: no index, no backfill (values arrive on the next pack re-seed / fresh install, same
caveat as ADR-052's `series`/`resolution_tier` — a plain re-seed does not backfill existing rows).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_artwork_aspect_crops"
down_revision: Union[str, Sequence[str], None] = "0005_artwork_series_resolution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent (defensive-migration pattern, ADR-035): skip a column a create_all-built DB already has.
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("artworks")}
    with op.batch_alter_table("artworks") as batch_op:
        if "aspect_crops_json" not in existing:
            batch_op.add_column(sa.Column("aspect_crops_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artworks") as batch_op:
        batch_op.drop_column("aspect_crops_json")
