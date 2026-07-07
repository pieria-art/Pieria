"""display now-playing columns (R1a)

Revision ID: 0003_display_now_playing
Revises: 0002_playback_session_unique
Create Date: 2026-07-07

Adds current_artwork_id + current_playlist to active_displays so the Remote and Devices views can show
"now showing" per display. Written by /next-image (the single selection brain used by both Canvas and
e-ink); liveness (last_seen_at) stays owned by the WS heartbeat / touch_active_display.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_display_now_playing"
down_revision: Union[str, Sequence[str], None] = "0002_playback_session_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent (defensive-migration pattern): skip columns a create_all-built DB already carries.
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("active_displays")}
    with op.batch_alter_table("active_displays") as batch_op:
        if "current_artwork_id" not in existing:
            batch_op.add_column(sa.Column("current_artwork_id", sa.Integer(), nullable=True))
        if "current_playlist" not in existing:
            batch_op.add_column(sa.Column("current_playlist", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("active_displays") as batch_op:
        batch_op.drop_column("current_playlist")
        batch_op.drop_column("current_artwork_id")
