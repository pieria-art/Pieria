"""
SQLAlchemy models for the Artwork Display Engine.
Phase 3: Many-to-Many relationship between Playlists and Artworks.
"""

from datetime import UTC, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

# Association Table for Many-to-Many relationship
# Includes display_order to allow unique sequencing per playlist
playlist_artwork = Table(
    "playlist_artwork",
    Base.metadata,
    Column("playlist_id", Integer, ForeignKey("playlists.id"), primary_key=True),
    Column("artwork_id", Integer, ForeignKey("artworks.id"), primary_key=True),
    Column("display_order", Integer, default=0)
)

class ActiveDisplayModel(Base):
    """
    Tracks which displays are currently active across multiple Uvicorn workers.
    """
    __tablename__ = "active_displays"

    display_id: Mapped[str] = mapped_column(String, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    # Now-playing: the artwork/collection currently on this display. Written by /next-image (the single
    # selection brain, used by both Canvas and e-ink); liveness (last_seen_at) stays heartbeat-owned.
    current_artwork_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_playlist: Mapped[Optional[str]] = mapped_column(String, nullable=True)

class RemoteCommandModel(Base):
    """
    A persistent command queue to bridge remote commands to targeted displays.
    """
    __tablename__ = "remote_commands"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    target_display: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    payload: Mapped[Optional[str]] = mapped_column(Text) # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))

class DisplayPlaybackSessionModel(Base):
    """
    Tracks playback state per display to ensure variety and resume capability.
    """
    __tablename__ = "display_playback_sessions"
    # A8: one row per (display, playlist). Without this, two concurrent first-requests (4 workers) could
    # each insert a session, after which .first() picks nondeterministically and the bag state splits.
    __table_args__ = (UniqueConstraint("display_id", "playlist_id", name="uq_playback_display_playlist"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    display_id: Mapped[str] = mapped_column(String, index=True)
    playlist_id: Mapped[int] = mapped_column(Integer, ForeignKey("playlists.id"), index=True)
    unplayed_artworks_json: Mapped[str] = mapped_column(Text, default="[]")
    last_sequential_index: Mapped[int] = mapped_column(Integer, default=-1)

class PlaylistModel(Base):
    """
    Table defining the artwork playlists.
    """
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    display_time: Mapped[int] = mapped_column(Integer, default=30)
    default_mode: Mapped[str] = mapped_column(String, default="ken-burns")
    shuffle: Mapped[bool] = mapped_column(Boolean, default=False)
    # True for "My Photos" albums (created via the personal Studio). Keeps the personal world
    # (My Photos chips) separate from Museum collections, which stay is_personal=False.
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)

    # Placard Timers (stored in seconds)
    placard_initial_wait_sec: Mapped[int] = mapped_column(Integer, default=5)
    placard_initial_show_sec: Mapped[int] = mapped_column(Integer, default=15)
    placard_interaction_show_sec: Mapped[int] = mapped_column(Integer, default=10)

    # Many-to-Many relationship
    artworks: Mapped[List["ArtworkModel"]] = relationship(
        secondary=playlist_artwork,
        back_populates="playlists",
        lazy="selectin",
        order_by="playlist_artwork.c.display_order"
    )

    def __repr__(self) -> str:
        return f"<Playlist(name='{self.name}')>"

class ArtworkModel(Base):
    """
    Table defining individual artwork metadata.
    Decoupled from specific playlists to allow library-wide management.
    """
    __tablename__ = "artworks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String, index=True)

    # Original Dimensions
    original_width: Mapped[int] = mapped_column(Integer, default=0)
    original_height: Mapped[int] = mapped_column(Integer, default=0)

    # Many-to-Many relationship
    playlists: Mapped[List["PlaylistModel"]] = relationship(
        secondary=playlist_artwork,
        back_populates="artworks"
    )

    # Phase 6: Telemetry & Director Data
    affinity_score: Mapped[float] = mapped_column(Float, default=1.0)
    skip_count: Mapped[int] = mapped_column(Integer, default=0)
    total_display_time: Mapped[int] = mapped_column(Integer, default=0)

    # VRA Core Metadata
    title: Mapped[Optional[str]] = mapped_column(String, index=True)
    agent_name: Mapped[Optional[str]] = mapped_column(String, index=True)
    agent_role: Mapped[Optional[str]] = mapped_column(String, default="Artist")
    creation_date: Mapped[Optional[str]] = mapped_column(String)
    cultural_context: Mapped[Optional[str]] = mapped_column(String)
    medium: Mapped[Optional[str]] = mapped_column(String)
    date_display: Mapped[Optional[str]] = mapped_column(String)
    description_narrative: Mapped[Optional[str]] = mapped_column(Text)
    tags: Mapped[Optional[str]] = mapped_column(String)   # comma-separated
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    # A personal photo (Studio → My Photos), not museum/catalog art: gates the jargon-free placard,
    # skips the museum AI pipeline, and keeps it out of Discover/publish.
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)

    # Provenance: where a seed/catalog/discovered work was fetched from (enables
    # re-download, dedup, and "already added" detection for the browseable catalog).
    source_url: Mapped[Optional[str]] = mapped_column(String, index=True)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, default='pending_review', index=True)

    # Crop Metadata (Stored in Original Pixels)
    crop_x: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    crop_y: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    crop_width: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    crop_height: Mapped[Optional[float]] = mapped_column(Float, default=0.0)

    # Focal point (normalized 0..1) — the visual subject the renderer keeps in frame when cropping
    # or panning to any aspect: the Ken Burns anchor + drift origin, and e-ink/Frame crop centering.
    # Default (0.5, 0.5) = image center = prior behavior (no regression for un-derived art).
    focal_x: Mapped[float] = mapped_column(Float, default=0.5)
    focal_y: Mapped[float] = mapped_column(Float, default=0.5)

    def __repr__(self) -> str:
        return f"<Artwork(filename='{self.filename}', status='{self.status}')>"

class DiscoveryQueueModel(Base):
    """
    Table for new art recommendations found by Art Scouts.
    """
    __tablename__ = "discovery_queue"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    source_url: Mapped[str] = mapped_column(String)
    thumbnail_url: Mapped[str] = mapped_column(String)
    proposed_title: Mapped[Optional[str]] = mapped_column(String)
    proposed_artist: Mapped[Optional[str]] = mapped_column(String)
    source_api: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default='pending') # pending, approved, rejected
    context_hints: Mapped[Optional[str]] = mapped_column(Text)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    search_session_id: Mapped[Optional[str]] = mapped_column(String, default=None)

class SettingsModel(Base):
    """
    Table for dynamic configuration, secrets, and API keys.
    """
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    setting_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    setting_value: Mapped[str] = mapped_column(String)


class SubscriptionModel(Base):
    """A federated Manifest v2 collection the user subscribed to by URL.

    Provenance lives HERE (in our DB), established at subscribe-time — never trusted from the
    manifest body. Every browsed item inherits its origin/trust from this row, so a third-party
    feed can never masquerade as a bundled/official collection (those load from disk, not this path).
    """
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    url: Mapped[str] = mapped_column(String, unique=True, index=True)
    collection_id: Mapped[Optional[str]] = mapped_column(String)   # from the manifest
    title: Mapped[Optional[str]] = mapped_column(String)
    publisher_id: Mapped[Optional[str]] = mapped_column(String)
    publisher_name: Mapped[Optional[str]] = mapped_column(String)
    publisher_url: Mapped[Optional[str]] = mapped_column(String)
    # 'community' (added by raw URL, trust-on-first-use) vs 'verified' (registry + signature, later).
    trust: Mapped[str] = mapped_column(String, default="community")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cached_manifest: Mapped[Optional[str]] = mapped_column(Text)    # last VALIDATED manifest JSON
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    last_synced: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_status: Mapped[Optional[str]] = mapped_column(String)      # 'ok' | 'error: …'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class PublisherCollectionModel(Base):
    """A draft collection being authored here for *publishing* as a Manifest v2 feed.

    The mirror image of SubscriptionModel: that consumes someone else's manifest; this AUTHORS one of
    our own. Items live as a JSON array (``items_json``) rather than normalized rows — the draft shape
    is 1:1 with the exported manifest, the field set is intentionally loose/forward-compatible (same
    stance as the validator), and there are no cross-collection item queries. Publisher *identity*
    (id/name/url + the Ed25519 keypair) is NOT here — it lives in SettingsModel, like every other
    secret, and is shared across all collections.
    """
    __tablename__ = "publisher_collections"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)   # the manifest "id"
    title: Mapped[str] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text)
    default_license: Mapped[Optional[str]] = mapped_column(String)
    cover_image: Mapped[Optional[str]] = mapped_column(String)           # URL shown with the collection name
    items_json: Mapped[str] = mapped_column(Text, default="[]")          # JSON array of item dicts
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
