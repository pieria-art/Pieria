"""gallery -> source collection link (Collections vs Galleries)

Revision ID: 0004_gallery_source_collection
Revises: 0003_display_now_playing
Create Date: 2026-07-18

Adds playlists.source_subscription_id — the explicit link from a Gallery (playlist) back to the
Collection (pack/sub subscription) it was minted from on download. Powers the "from your <name>
Collection" source line + the Collection<->Gallery cross-links, and survives a gallery rename (a
name-match would not). NULL for a user-built gallery.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_gallery_source_collection"
down_revision: Union[str, Sequence[str], None] = "0003_display_now_playing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent (defensive-migration pattern): skip if a create_all-built DB already carries it.
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("playlists")}
    if "source_subscription_id" not in existing:
        with op.batch_alter_table("playlists") as batch_op:
            batch_op.add_column(sa.Column("source_subscription_id", sa.Integer(), nullable=True))
            batch_op.create_index("ix_playlists_source_subscription_id", ["source_subscription_id"])

    # Backfill galleries installed BEFORE this link existed: a playlist minted from a pack shares the
    # pack subscription's title (that's how _install_collection named it), so match on name -> pack sub.
    op.execute("""
        UPDATE playlists
           SET source_subscription_id = (
               SELECT s.id FROM subscriptions s
                WHERE s.title = playlists.name AND s.url LIKE 'pack:%' LIMIT 1)
         WHERE source_subscription_id IS NULL
           AND EXISTS (
               SELECT 1 FROM subscriptions s
                WHERE s.title = playlists.name AND s.url LIKE 'pack:%')
    """)


def downgrade() -> None:
    with op.batch_alter_table("playlists") as batch_op:
        batch_op.drop_index("ix_playlists_source_subscription_id")
        batch_op.drop_column("source_subscription_id")
