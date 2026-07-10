"""Playlist-linking helper shared across routers/library.py (bulk-link route) and the studio
personal-photo upload that stays in app.py — cross-cutting, so it lives here rather than forcing
a routers -> app import.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import playlist_artwork


def _link_artwork_to_playlist(db: Session, playlist_id: int, artwork_id: int) -> None:
    """Append an artwork to a playlist (idempotent), preserving append order."""
    exists = db.execute(select(playlist_artwork).where(
        playlist_artwork.c.playlist_id == playlist_id,
        playlist_artwork.c.artwork_id == artwork_id)).first()
    if exists:
        return
    order = len(db.execute(select(playlist_artwork.c.artwork_id).where(
        playlist_artwork.c.playlist_id == playlist_id)).all())
    db.execute(playlist_artwork.insert().values(
        playlist_id=playlist_id, artwork_id=artwork_id, display_order=order))
    db.commit()
