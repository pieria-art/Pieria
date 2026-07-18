"""FastAPI lifespan (boot) machinery — leader election, migrations, filesystem sync, factory seed,
and the Canvas cache warmer. Extracted verbatim from app.py (Phase 4 of the app-split refactor).
"""

import asyncio
import fcntl
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import federation
import frame_push
from config import ARTWORK_ROOT, LIBRARY_DIR
from core.downloads import _download_image_to_library, _focal_xy
from core.media import render_canvas_image
from core.playback import _frame_select
from core.settings_util import _upsert_setting
from database import SessionLocal
from db_migrate import run_migrations
from manifest_validator import validate_manifest
from models import (
    ArtworkModel,
    DisplayPlaybackSessionModel,
    PlaylistModel,
    SettingsModel,
    SubscriptionModel,
    playlist_artwork,
)

logger = logging.getLogger("artwork-display-api")

# ADR-038: the manifest tools/build_pack.py bakes into a self-contained appliance art-pack. When
# present, boot mints its collections straight from local masters (pre_seed_from_pack) instead of
# downloading factory_seed.json live (run_factory_seed) — see the leader-only boot section below.
PACK_MANIFEST = ARTWORK_ROOT / "pack-manifest.json"
# ADR-044: the unified-ingestion pack layout — a pack-index listing per-collection signed Manifest v2
# feeds. When present, boot installs each as a VERIFIED LOCAL SUBSCRIPTION (the same path a third-party
# publisher uses), superseding the v1 pre_seed_from_pack above.
PACK_INDEX = ARTWORK_ROOT / "pack-index.json"


async def warm_all_canvas_cache() -> None:
    """Leader boot task: pre-render the capped display image for every approved artwork so the Canvas
    never stalls on first display (esp. huge museum originals on a Pi). Sequential — one encode at a
    time — to avoid a CPU storm while the server is also serving; `render_canvas_image` skips anything
    already cached, so reruns are cheap. Best-effort per item."""
    db = SessionLocal()
    try:
        arts = (db.query(ArtworkModel.id, ArtworkModel.filename)
                .filter(ArtworkModel.status == "approved").all())
    finally:
        db.close()
    logger.info(f"[Warm] pre-rendering display derivatives for {len(arts)} artworks...")
    done = 0
    for art_id, filename in arts:
        try:
            await run_in_threadpool(render_canvas_image, LIBRARY_DIR / filename, art_id)
            done += 1
        except Exception as e:
            logger.warning(f"[Warm] art {art_id} ({filename}): {e}")
    logger.info(f"[Warm] display cache warm complete ({done}/{len(arts)}).")

def sync_db_with_filesystem(db: Session) -> None:
    if not ARTWORK_ROOT.exists():
        ARTWORK_ROOT.mkdir(parents=True, exist_ok=True)
    if not LIBRARY_DIR.exists():
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    valid_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    for item in ARTWORK_ROOT.iterdir():
        # Skip internal dirs (underscore-prefixed: _Library canonical store, _derivatives display cache).
        # They are NOT collections — enumerating them here would mint a bogus playlist and absorb cache files.
        if item.is_dir() and not item.name.startswith("_"):
            playlist = db.query(PlaylistModel).filter(PlaylistModel.name == item.name).first()
            if not playlist:
                playlist = PlaylistModel(name=item.name)
                db.add(playlist); db.commit(); db.refresh(playlist)

            for file_path in item.iterdir():
                if file_path.suffix.lower() in valid_extensions:
                    dest_path = LIBRARY_DIR / file_path.name
                    if not dest_path.exists():
                        shutil.move(file_path, dest_path)

                    artwork = db.query(ArtworkModel).filter(ArtworkModel.filename == file_path.name).first()
                    if not artwork:
                        with Image.open(dest_path) as img:
                            w, h = img.size
                        artwork = ArtworkModel(
                            filename=file_path.name,
                            original_width=w, original_height=h,
                            status='approved'
                        )
                        db.add(artwork); db.commit(); db.refresh(artwork)

                    existing_link = db.execute(
                        select(playlist_artwork).where(
                            playlist_artwork.c.playlist_id == playlist.id,
                            playlist_artwork.c.artwork_id == artwork.id
                        )
                    ).first()

                    if not existing_link:
                        db.execute(playlist_artwork.insert().values(
                            playlist_id=playlist.id,
                            artwork_id=artwork.id,
                            display_order=0
                        ))
            db.commit()

def pre_seed_from_pack(db: Session) -> bool:
    """Boot-time consumer of tools/build_pack.py's pack-manifest.json (ADR-038): when the appliance
    ships with a baked-in art-pack, mint its collections into playlists with masters already local
    (no downloads) — the offline counterpart to run_factory_seed's live-download path.

    Returns False when there's no pack (PACK_MANIFEST absent) so the caller falls back to
    run_factory_seed; True once seeded (including "already seeded" on a repeat boot).
    """
    if not PACK_MANIFEST.exists():
        return False

    if db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first():
        return True  # idempotency guard — distinct from is_seed, which run_factory_seed also sets

    manifest = json.loads(PACK_MANIFEST.read_text())
    collections = manifest.get("collections", [])
    logger.info(f"[PackSeed] Pre-seeding {len(collections)} collection(s) from pack-manifest.json...")

    default_title = None
    masterpieces_present = False

    for col in collections:
        title = col.get("title") or col.get("id")
        if not title:
            continue
        if default_title is None:
            default_title = title
        if title == "Masterpieces":
            masterpieces_present = True

        playlist = db.query(PlaylistModel).filter(PlaylistModel.name == title).first()
        if not playlist:
            playlist = PlaylistModel(name=title, is_personal=False)
            db.add(playlist); db.commit(); db.refresh(playlist)

        # Highest-ranked work first (display_order=0) within its own collection.
        items = sorted(col.get("items", []), key=lambda it: it.get("featured_rank", 50), reverse=True)

        for idx, item in enumerate(items):
            filename = item.get("filename")
            if not filename:
                continue
            master = LIBRARY_DIR / filename
            if not master.exists():
                logger.warning(f"[PackSeed] '{title}': missing master {filename!r} — skipping")
                continue

            source_url = item.get("source_url") or ""
            artwork = None
            if source_url:
                artwork = db.query(ArtworkModel).filter(ArtworkModel.source_url == source_url).first()
            if artwork is None:
                artwork = db.query(ArtworkModel).filter(ArtworkModel.filename == filename).first()

            if artwork is None:
                fx, fy = _focal_xy(item)
                rank = item.get("featured_rank", 50)
                artwork = ArtworkModel(
                    filename=filename,
                    status="approved",
                    title=item.get("title"), agent_name=item.get("agent_name"),
                    agent_role=item.get("agent_role"), creation_date=item.get("creation_date"),
                    cultural_context=item.get("cultural_context"), medium=item.get("medium"),
                    date_display=item.get("date_display"),
                    description_narrative=item.get("description_narrative"),
                    tags=item.get("tags"), is_seed=True, source_url=source_url,
                    focal_x=fx, focal_y=fy,
                    affinity_score=round(0.5 + rank / 100.0, 3),
                )
                db.add(artwork); db.commit(); db.refresh(artwork)

            existing_link = db.execute(
                select(playlist_artwork).where(
                    playlist_artwork.c.playlist_id == playlist.id,
                    playlist_artwork.c.artwork_id == artwork.id,
                )
            ).first()
            if not existing_link:
                db.execute(playlist_artwork.insert().values(
                    playlist_id=playlist.id, artwork_id=artwork.id, display_order=idx
                ))

        db.commit()

    # Honor "Masterpieces" (the paintings-only first-glimpse) as the canonical default rotation when
    # present; else the first collection — same setting key routers/settings.py's default-playlist
    # get/set uses, so the Canvas picks it up.
    chosen_default = "Masterpieces" if masterpieces_present else default_title
    if chosen_default:
        _upsert_setting(db, "default_playlist", chosen_default)

    _upsert_setting(db, "pack_seeded", manifest.get("version", "v1"))
    db.commit()

    logger.info("[PackSeed] Pre-seed complete.")
    return True


def _install_collection(db: Session, cid: str, manifest: dict) -> str | None:
    """Install ONE collection's signed Manifest v2 as a verified LOCAL subscription + mint its playlist and
    ArtworkModels from the LOCAL masters (array order == fame order), zero-network. Idempotent: upserts the
    subscription, dedups artworks by source_url/filename, reuses the playlist by name. Returns the playlist
    title, or None if the manifest is invalid. Shared by boot (install_pack_subscriptions, the baked Core)
    and the runtime append path (install_downloaded_collection, ADR-040 #4 modular packs)."""
    errors = validate_manifest(manifest)
    if errors:
        logger.warning(f"[PackInstall] {cid!r} manifest invalid, skipping: {errors[:3]}")
        return None

    title = manifest.get("title") or cid
    pub = manifest.get("publisher") or {}

    # 1) Upsert the local subscription row — provenance/trust live in OUR DB, not the manifest body.
    sub_url = f"pack:{cid}"
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.url == sub_url).first()
    if sub is None:
        sub = SubscriptionModel(url=sub_url)
        db.add(sub)
    sub.collection_id = manifest.get("id")
    sub.title = title
    sub.publisher_id = pub.get("id")
    sub.publisher_name = pub.get("name")
    sub.publisher_url = pub.get("url")
    sub.trust = federation.assess_trust(manifest)   # 'verified' iff the key is registry-trusted
    sub.enabled = True
    sub.cached_manifest = json.dumps(manifest)
    sub.item_count = len(manifest.get("items", []))
    sub.last_status = "ok"
    sub.last_synced = datetime.now(UTC)
    db.commit()

    # 2) Mint a Gallery (playlist) + artworks from LOCAL masters (array order == fame order). Link the
    #    Gallery back to this Collection (subscription) so the UI can show "from your <name> Collection".
    playlist = db.query(PlaylistModel).filter(PlaylistModel.name == title).first()
    if not playlist:
        playlist = PlaylistModel(name=title, is_personal=False)
        db.add(playlist); db.commit(); db.refresh(playlist)
    if playlist.source_subscription_id != sub.id:
        playlist.source_subscription_id = sub.id
        db.commit()

    items = manifest.get("items", [])
    n = len(items)
    for idx, item in enumerate(items):
        cat = federation.manifest_item_to_catalog(item)
        local_file = cat.get("local_file")
        if not local_file:
            continue
        if not (LIBRARY_DIR / local_file).exists():
            logger.warning(f"[PackInstall] '{title}': missing master {local_file!r} — skipping")
            continue
        source_url = cat.get("source_url") or f"pack:{local_file}"
        artwork = (db.query(ArtworkModel).filter(ArtworkModel.source_url == source_url).first()
                   or db.query(ArtworkModel).filter(ArtworkModel.filename == local_file).first())
        if artwork is None:
            fx, fy = _focal_xy(cat)
            # position → affinity (array is fame-sorted): first work ~1.0, last ~0.5, mirroring
            # pre_seed's 0.5+rank/100 weighting now that featured_rank is expressed as order.
            affinity = round(0.5 + (n - idx) / max(n, 1) * 0.5, 3)
            artwork = ArtworkModel(
                filename=local_file, status="approved",
                title=cat.get("title"), agent_name=cat.get("agent_name"),
                agent_role=cat.get("agent_role"), creation_date=cat.get("creation_date"),
                cultural_context=cat.get("cultural_context"), medium=cat.get("medium"),
                date_display=cat.get("date_display"),
                description_narrative=cat.get("description_narrative"),
                tags=cat.get("tags"), is_seed=True, source_url=source_url,
                focal_x=fx, focal_y=fy, affinity_score=affinity,
            )
            db.add(artwork); db.commit(); db.refresh(artwork)

        existing_link = db.execute(select(playlist_artwork).where(
            playlist_artwork.c.playlist_id == playlist.id,
            playlist_artwork.c.artwork_id == artwork.id)).first()
        if not existing_link:
            db.execute(playlist_artwork.insert().values(
                playlist_id=playlist.id, artwork_id=artwork.id, display_order=idx))
    db.commit()
    return title


def install_pack_subscriptions(db: Session) -> bool:
    """ADR-044 unified ingestion: install the bundled Core pack as VERIFIED LOCAL SUBSCRIPTIONS — the
    same path a third-party publisher uses — superseding the bespoke pre_seed_from_pack. Reads
    pack-index.json and installs each collection's signed Manifest v2 via `_install_collection`
    (validate + assess trust + upsert subscription + mint playlist/artworks from LOCAL masters). Zero-network.

    Returns False when there's no v2 pack (pack-index.json absent) so the caller falls back to the v1
    pre_seed_from_pack; True once installed (including 'already installed' on a repeat boot)."""
    if not PACK_INDEX.exists():
        return False
    if db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first():
        return True  # idempotency guard (shared with pre_seed_from_pack — either path seeds once)

    index = json.loads(PACK_INDEX.read_text())
    collections = index.get("collections", [])
    logger.info(f"[PackInstall] Installing {len(collections)} collection(s) as verified local subscriptions...")

    default_title = None
    for col in collections:
        cid = col.get("id")
        mpath = ARTWORK_ROOT / col.get("manifest", f"_manifests/{cid}.json")
        if not mpath.exists():
            logger.warning(f"[PackInstall] missing manifest {mpath} — skipping {cid!r}")
            continue
        title = _install_collection(db, cid, json.loads(mpath.read_text()))
        if title is None:
            continue
        if default_title is None or col.get("default"):
            default_title = title

    if default_title:
        _upsert_setting(db, "default_playlist", default_title)
    _upsert_setting(db, "pack_seeded", f"v2:{index.get('pack_version', '2')}")
    db.commit()
    logger.info("[PackInstall] Install complete.")
    return True


def install_downloaded_collection(db: Session, cid: str) -> bool:
    """Runtime APPEND (ADR-040 #4 modular packs): install one on-demand-downloaded collection whose signed
    manifest + local masters were just extracted under ARTWORK_ROOT — WITHOUT re-seeding the rest (multi-pack
    *append*, not replace) and without touching the default playlist. Idempotent. Returns True if installed
    (or already present), False if its manifest is absent/invalid."""
    mpath = ARTWORK_ROOT / "_manifests" / f"{cid}.json"
    if not mpath.exists():
        logger.warning(f"[PackInstall] downloaded collection {cid!r}: no manifest at {mpath}")
        return False
    logger.info(f"[PackInstall] Appending downloaded collection {cid!r}...")
    title = _install_collection(db, cid, json.loads(mpath.read_text()))
    db.commit()
    return title is not None


def uninstall_collection(db: Session, cid: str) -> dict | None:
    """Tier-2 'Remove collection' (Curated Art): fully UNINSTALL a pack — the inverse of
    `_install_collection`. Drops the `pack:<cid>` subscription + its playlist, and deletes ONLY the
    artworks that become unlinked (a master shared into Masterpieces or a user's custom playlist stays;
    dedup means one ArtworkModel can back several collections). Never touches personal photos. Frees the
    masters' disk. If the collection was the default playlist, the default is reassigned to another
    Museum collection (or cleared). Returns a summary, or None if `pack:<cid>` isn't installed.

    NOT the same as deleting the collection's playlist (Tier 1, `DELETE /playlists/{id}`), which keeps
    every work in the library — this reclaims the art."""
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.url == f"pack:{cid}").first()
    if sub is None:
        return None
    title = sub.title or cid
    playlist = db.query(PlaylistModel).filter(
        PlaylistModel.name == title, PlaylistModel.is_personal.is_(False)).first()

    artworks_removed = 0
    if playlist is not None:
        # The artworks this playlist links, captured BEFORE we drop the links.
        art_ids = [row[0] for row in db.execute(select(playlist_artwork.c.artwork_id).where(
            playlist_artwork.c.playlist_id == playlist.id)).all()]
        db.execute(delete(playlist_artwork).where(playlist_artwork.c.playlist_id == playlist.id))
        db.commit()

        for aid in art_ids:
            art = db.query(ArtworkModel).filter(ArtworkModel.id == aid).first()
            if art is None or art.is_personal:
                continue  # a user photo could never belong to a pack, but never delete one regardless
            still_linked = db.execute(select(playlist_artwork.c.playlist_id).where(
                playlist_artwork.c.artwork_id == aid).limit(1)).first()
            if still_linked is None:  # orphaned by this uninstall -> reclaim file + row
                f_path = LIBRARY_DIR / art.filename
                if f_path.is_symlink() or f_path.exists():
                    try:
                        f_path.unlink()
                    except OSError as e:
                        logger.warning(f"[PackUninstall] could not unlink {f_path}: {e}")
                db.delete(art)
                artworks_removed += 1
        db.commit()

        # Drop any per-display playback bags for this playlist (no FK cascade) before removing it.
        db.execute(delete(DisplayPlaybackSessionModel).where(
            DisplayPlaybackSessionModel.playlist_id == playlist.id))
        db.delete(playlist)
        db.commit()

    # If this was the default collection, hand the default to another Museum collection (or clear it).
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    if default is not None and default.setting_value == title:
        nxt = db.query(PlaylistModel).filter(PlaylistModel.is_personal.is_(False)).order_by(
            PlaylistModel.id).first()
        default.setting_value = nxt.name if nxt else ""
        db.commit()

    db.delete(sub)
    db.commit()
    logger.info(f"[PackUninstall] removed {cid!r} ({title!r}): {artworks_removed} artwork(s) reclaimed.")
    return {"cid": cid, "title": title, "artworks_removed": artworks_removed}


async def run_factory_seed(db: Session):
    """Parses factory_seed.json and injects masterpieces if library is empty."""
    seed_file = Path("static/factory_seed.json")
    if not seed_file.exists(): return

    existing = db.query(ArtworkModel).filter(ArtworkModel.is_seed == True).first()
    if existing: return

    try:
        import json
        with open(seed_file) as f:
            seeds = json.load(f)

        logger.info(f"[Bootstrapper] Injecting {len(seeds)} Masterpieces from Factory Seed...")

        async def perform_downloads(seed_items: list):
            db_local = SessionLocal()
            try:
                await asyncio.sleep(2)
                for idx, item in enumerate(seed_items):
                    await asyncio.sleep(2.0)
                    try:
                        pl_name = item.get("playlist", "The Masterpieces")
                        playlist = db_local.query(PlaylistModel).filter(PlaylistModel.name == pl_name).first()
                        if not playlist:
                            playlist = PlaylistModel(name=pl_name)
                            db_local.add(playlist); db_local.commit(); db_local.refresh(playlist)
                            (ARTWORK_ROOT / pl_name).mkdir(parents=True, exist_ok=True)

                        filename = f"seed_{idx}_{item.get('title', 'art').replace(' ','_').lower()[:15]}"
                        logger.info(f"[Bootstrapper] Downloading '{filename}'...")

                        # Shared robust downloader (UA + 429 retry + validation).
                        try:
                            dest_path, safe_name, w, h = await _download_image_to_library(
                                item.get("source_url"), filename=filename)
                        except HTTPException as e:
                            logger.error(f"[Bootstrapper] Failed download {filename}: {e.detail}")
                            continue

                        pl_path = ARTWORK_ROOT / pl_name / safe_name
                        # Remove stale symlink before creating new one
                        if pl_path.is_symlink() or pl_path.exists():
                            pl_path.unlink()
                        try: os.symlink(dest_path.resolve(), pl_path)
                        except OSError: shutil.copy(dest_path, pl_path)

                        sfx, sfy = _focal_xy(item)
                        artwork = ArtworkModel(
                            filename=safe_name, original_width=w, original_height=h,
                            crop_width=float(w), crop_height=float(h),
                            status='approved',
                            title=item.get("title"), agent_name=item.get("agent_name"),
                            agent_role=item.get("agent_role"), creation_date=item.get("creation_date"),
                            cultural_context=item.get("cultural_context"), medium=item.get("medium"),
                            date_display=item.get("date_display"), description_narrative=item.get("description_narrative"),
                            tags=item.get("tags"), is_seed=True,
                            focal_x=sfx, focal_y=sfy,
                        )
                        db_local.add(artwork); db_local.commit(); db_local.refresh(artwork)

                        try:
                            db_local.execute(playlist_artwork.insert().values(
                                playlist_id=playlist.id, artwork_id=artwork.id, display_order=idx
                            ))
                            db_local.commit()
                        except Exception:
                            db_local.rollback()  # playlist_artwork may already exist

                        logger.info(f"[Bootstrapper] ✓ Seeded '{item.get('title')}' → {pl_name}")

                    except Exception as inner_e: logger.error(f"[Bootstrapper] Item error: {inner_e}")
            finally: db_local.close()

        asyncio.create_task(perform_downloads(seeds))

    except Exception as e:
        logger.error(f"[Bootstrapper] Failed to parse factory_seed.json: {e}")


async def _seed_default_collection(registry_url: str, cid: str):
    """Background: download+install the OOB default collection from R2, then set it as the default
    playlist and mark the pack seeded. Own real, verified art — no live Wikimedia dependency."""
    from core import pack_fetch  # lazy: avoids the pack_fetch <-> lifespan import cycle
    db = SessionLocal()
    client = pack_fetch.new_client()
    try:
        res = await pack_fetch.install_collection_from_registry(db, client, registry_url, cid)
        if not res.get("ok"):
            logger.error(f"[Seed] OOB install of {cid!r} failed: {res.get('error')}")
            return
        # _install_collection already minted the '<title>' playlist + artworks; make it the default.
        sub = db.query(SubscriptionModel).filter(SubscriptionModel.url == f"pack:{cid}").first()
        title = sub.title if sub and sub.title else cid
        _upsert_setting(db, "default_playlist", title)
        _upsert_setting(db, "pack_seeded", "v2:registry")
        db.commit()
        logger.info(f"[Seed] OOB ready — '{title}' installed from R2 + set as default playlist ({res.get('trust')}).")
    except Exception as e:  # noqa: BLE001 — seeding must never wedge the box
        logger.error(f"[Seed] OOB default-collection error: {e}", exc_info=True)
    finally:
        await client.aclose()
        db.close()


async def seed_from_registry(db: Session) -> bool:
    """OOB "art on screen in 5 minutes" (ADR-038/040 #4): with NO baked pack, pull the registry's DEFAULT
    collection (Masterpieces) from R2, install it — which mints the 'Masterpieces' playlist — set it as the
    default playlist, and mark seeded. So a fresh `docker compose up` owns real, *verified* art instead of
    live-downloading the Wikimedia factory seed (retires source-rot on first-run too). The download runs in
    the background so boot stays fast; art appears when it lands. Additional collections come via the card.

    Returns True if seeding was initiated (caller SKIPS the factory-seed fallback); False if the registry is
    unreachable/empty (caller falls back to run_factory_seed — a purist/offline "works without our infra")."""
    from config import PACK_REGISTRY_URL
    from core import pack_fetch  # lazy: import cycle
    if db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first():
        return True  # already seeded (idempotent)

    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_registry_url").first()
    registry_url = row.setting_value if row and row.setting_value else PACK_REGISTRY_URL

    client = pack_fetch.new_client()
    try:
        registry = await pack_fetch.fetch_registry(client, registry_url)
    except Exception as e:  # noqa: BLE001 — registry unreachable => fall back to the factory seed
        logger.warning(f"[Seed] registry {registry_url} unreachable ({type(e).__name__}); using factory seed.")
        return False
    finally:
        await client.aclose()

    cols = registry.get("collections", [])
    default_id = (registry.get("default")
                  or ("masterpieces" if any(c.get("id") == "masterpieces" for c in cols) else None)
                  or (registry.get("core") or [None])[0]
                  or (cols[0]["id"] if cols else None))
    if not default_id:
        logger.warning("[Seed] registry has no installable default collection; using factory seed.")
        return False

    logger.info(f"[Seed] OOB: pulling default collection {default_id!r} from {registry_url} (background)...")
    asyncio.create_task(_seed_default_collection(registry_url, default_id))
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events for FastAPI application with multi-worker concurrency locks."""

    # Leader Election using fcntl: the first worker grabs the exclusive non-blocking lock
    # and runs exclusive boot tasks; the other workers get BlockingIOError and skip them.
    # We deliberately never unlock — the OS releases the flock when the worker process exits,
    # so a slightly delayed follower can't grab it mid-boot and race the migrations.
    lock_file = open("/tmp/screen_docent_startup.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("[Startup] Follower worker initialized. Skipping exclusive boot tasks.")
        yield
        return

    logger.info("[Startup] Leader elected. Running exclusive boot tasks (migrations, filesystem sync)...")

    # 1) Schema: Alembic is the single source of truth (create_all no longer runs at boot).
    #    A migration failure MUST halt startup — it is caught at deploy, not by a user's
    #    black screen. Deliberately NOT wrapped in a swallowing try/except (see ADR-035).
    #    When the Docker entrypoint already migrated single-process (SD_MIGRATIONS_DONE=1), skip it here:
    #    re-running run_migrations under 4 live workers deadlocks the SQLite DB (ADR-037).
    if os.getenv("SD_MIGRATIONS_DONE"):
        logger.info("Alembic migrations already applied at entrypoint (SD_MIGRATIONS_DONE); skipping.")
    else:
        logger.info("Running Alembic migrations...")
        run_migrations()
        logger.info("Alembic migrations complete.")

    # 2) Best-effort init: a hiccup in filesystem sync / seed / warmers should not wedge the
    #    whole box, so these stay tolerant (unlike migrations above).
    try:
        db = SessionLocal()
        try:
            sync_db_with_filesystem(db)
            # ADR-038: a bundled appliance art-pack pre-seeds from local masters, no network. Only
            # fall back to the live-download factory seed when there's no pack to consume. Guarded
            # separately from the tolerant block below — a bad manifest must never block the fallback.
            try:
                # ADR-044: prefer the unified v2 install (verified local subscriptions); fall back to
                # the v1 pre_seed for an older pack that ships only pack-manifest.json.
                has_pack = install_pack_subscriptions(db) or pre_seed_from_pack(db)
                # ADR-040 #4: no baked pack (vanilla `docker compose`)? Own art via R2 — pull the default
                # collection (Masterpieces) + set it default. Only if that also fails do we live-seed.
                if not has_pack:
                    has_pack = await seed_from_registry(db)
            except Exception as e:
                logger.error(f"[PackSeed] Non-fatal error during pack install/pre-seed: {e}", exc_info=True)
                has_pack = False
            if not has_pack:
                await run_factory_seed(db)
        finally:
            db.close()

        # Pre-render the capped Canvas derivatives in the background so the display never stalls on
        # the one-time encode of a huge original (leader-only; runs while the server serves traffic).
        asyncio.create_task(warm_all_canvas_cache())

        # Leader-only: the Samsung Frame TV pusher. Running it solely in the leader avoids
        # firing it once per uvicorn worker. No-op until enabled in Settings → Frame TV.
        asyncio.create_task(frame_push.frame_push_loop(_frame_select))
        logger.info("[Startup] Frame TV push loop scheduled (leader).")
    except Exception as e:
        logger.error(f"[Startup] Non-fatal error during initialization: {e}", exc_info=True)

    yield
