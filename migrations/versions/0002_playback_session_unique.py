"""playback session composite-unique (A8)

Revision ID: 0002_playback_session_unique
Revises: 0001_baseline
Create Date: 2026-07-06

Adds UNIQUE(display_id, playlist_id) to display_playback_sessions so two concurrent first-requests for
the same display+playlist (4 uvicorn workers) can't each insert a row and split the bag-shuffle state.
Any pre-existing duplicates are collapsed (keep the lowest id) before the constraint is created. The
get-or-create in app.get_next_image is made IntegrityError-safe to cooperate with this constraint.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_playback_session_unique"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent (defensive-migration pattern): a DB whose schema was historically built by create_all
    # may already carry this unique constraint, so skip if the (display_id, playlist_id) columns are
    # already covered. The real deployed box (baseline built pre-constraint) has none → we add it.
    insp = sa.inspect(op.get_bind())
    target = {"display_id", "playlist_id"}
    already = any(set(uc["column_names"]) == target
                  for uc in insp.get_unique_constraints("display_playback_sessions"))
    if already:
        return
    # Collapse any existing duplicate (display_id, playlist_id) rows, keeping the lowest id, so the
    # UNIQUE constraint can be created on real-world data.
    op.execute(
        "DELETE FROM display_playback_sessions WHERE id NOT IN ("
        "  SELECT MIN(id) FROM display_playback_sessions GROUP BY display_id, playlist_id"
        ")"
    )
    # SQLite can't ALTER TABLE ADD CONSTRAINT — batch mode recreates the table with the constraint.
    with op.batch_alter_table("display_playback_sessions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_playback_display_playlist", ["display_id", "playlist_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("display_playback_sessions") as batch_op:
        batch_op.drop_constraint("uq_playback_display_playlist", type_="unique")
