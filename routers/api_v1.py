"""Public API v1 (ADR-147) — the one stable, documented, token-authenticated contract that the Home
Assistant integration, the MCP server and any third-party control are built against.

A SEPARATE FastAPI instance mounted at `/api/v1` (see app.py): it owns its own `/api/v1/openapi.json`
+ `/api/v1/docs`, so the published contract is v1 only and never the internal surface. Within v1 we
only ever ADD (new endpoints, new optional fields, new enum values) — never rename, remove or retype.

This module is a thin facade: every endpoint calls an existing core function (core.playback,
core.settings_util, core.licensing, the Settings KV) rather than re-deriving behaviour. Two rules it
must never break:
  * Reads are PURE. Nothing here calls select_next_image / /next-image / the e-ink pull — those
    advance the display's shuffle state. Now-playing comes from the active_displays row only.
  * Every route needs `Authorization: Bearer <token>` (core.api_tokens). 401 = missing/invalid/revoked
    (with WWW-Authenticate), 403 = valid token lacking the scope. Errors are always
    `{"error": {"code", "message"}}`.
"""

import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
import core.licensing as core_licensing
import host_health
from core import api_tokens
from core.playback import (
    _as_utc,
    _is_live,
    display_updated_at,
    is_display_paused,
    known_displays,
    placard_metadata,
    queue_remote_command,
    set_display_paused,
    set_pending_show,
    touch_display_updated,
)
from core.settings_util import (
    ScheduleError,
    _load_schedule,
    _upsert_setting,
    apply_schedule_patch,
    get_quiet_override,
    quiet_status,
    resolve_schedule_state,
    set_quiet_override,
)
from database import get_db
from models import (
    ActiveDisplayModel,
    ApiTokenModel,
    ArtworkModel,
    PlaylistModel,
    SettingsModel,
    playlist_artwork,
)

logger = logging.getLogger("artwork-display-api")

API_VERSION = 1
# Capability list `GET /info` advertises; clients feature-detect on it. Additive only.
API_FEATURES = ["displays", "commands", "pause", "show", "quiet", "schedule", "search"]
MAX_ID = 2**63 - 1          # SQLite INTEGER ceiling: anything larger is a 422, never a 500
MAX_OFFSET = 1_000_000
def IdPath():
    return Path(..., ge=1, le=MAX_ID)   # a fresh FieldInfo per parameter (FastAPI binds the name onto it)
SERVER_ID_KEY = "server_id"
DISPLAY_MODE_PREFIX = "display_mode:"   # last render mode commanded through this API, per display


# ---------------------------------------------------------------------------------------------------
# Errors — one envelope for everything: {"error": {"code", "message"}}
# ---------------------------------------------------------------------------------------------------

class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable code, e.g. `unauthorized`, `not_live`.")
    message: str = Field(description="Human-readable explanation. Do not parse it.")


class ErrorResponse(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[dict] = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers


_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 422: "validation_error",
                 429: "rate_limited"}


def _envelope(status: int, code: str, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers=headers)


async def _api_error_handler(_request: Request, exc: ApiError):
    return _envelope(exc.status, exc.code, exc.message, exc.headers)


async def _http_error_handler(_request: Request, exc: StarletteHTTPException):
    return _envelope(exc.status_code, _STATUS_CODES.get(exc.status_code, "error"),
                     str(exc.detail), getattr(exc, "headers", None))


async def _validation_error_handler(_request: Request, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return _envelope(422, "validation_error", "; ".join(parts) or "invalid request")


async def _unhandled_error_handler(_request: Request, exc: Exception):
    logger.error("api v1 unhandled error: %s", exc, exc_info=exc)
    return _envelope(500, "internal_error", "internal server error")


# ---------------------------------------------------------------------------------------------------
# Auth — Bearer token, scope per route
# ---------------------------------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth",
                     description="A Pieria API token (`pieria_...`) minted in Admin -> API & Integrations. "
                                 "Scopes: `read` (all GETs) and `control` (commands, PATCH /schedule); "
                                 "they are independent.")
_WWW_AUTH = {"WWW-Authenticate": 'Bearer realm="pieria-api"'}


def _authenticate(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
                  db: Session = Depends(get_db)) -> ApiTokenModel:
    if creds is None or not creds.credentials:
        raise ApiError(401, "unauthorized", "missing bearer token", _WWW_AUTH)
    row = api_tokens.verify_token(db, creds.credentials)
    if row is None:
        raise ApiError(401, "unauthorized", "invalid or revoked token", _WWW_AUTH)
    return row


def _require(scope: str):
    def dep(token: ApiTokenModel = Depends(_authenticate)) -> ApiTokenModel:
        if scope not in api_tokens.token_scopes(token):
            raise ApiError(403, "insufficient_scope", f"this token lacks the '{scope}' scope")
        return token
    return dep


require_read = _require("read")
require_control = _require("control")


# ---------------------------------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------------------------------

class RenderMode(StrEnum):
    ken_burns = "ken-burns"
    static_crop = "static-crop"
    contain_matte = "contain-matte"


class CommandAction(StrEnum):
    """A1 set + `pause` / `resume` (A2; additive — clients must tolerate new values)."""
    next = "next"
    previous = "previous"
    pause = "pause"
    resume = "resume"
    show_placard = "show_placard"
    set_playlist = "set_playlist"
    set_mode = "set_mode"


class Info(BaseModel):
    name: str = Field(description='Always "Pieria".', examples=["Pieria"])
    version: str = Field(description="The server's release version.", examples=["1.0.6"])
    api_version: int = Field(description="Major version of this API. Always 1 under /api/v1.")
    appliance_mode: bool = Field(description="True on a Pieria appliance (Raspberry Pi image).")
    server_id: str = Field(description="UUID minted once on first use and stable across restarts and "
                                       "upgrades (restored with a backup). Use it as the unique id of this server.")
    display_name: Optional[str] = Field(None, description="The appliance's configured name, if any.")
    api_features: list[str] = Field(description="Capabilities this server offers (e.g. `pause`, `show`, "
                                                "`quiet`). Feature-detect on this, not on the version; new "
                                                "values are only ever added.")
    token_scopes: list[str] = Field(description="The scopes (`read`, `control`) of the token making this call.")


class ArtworkCompact(BaseModel):
    id: int
    title: Optional[str] = None
    artist: Optional[str] = None
    year: Optional[str] = Field(None, description="Display date as the museum gave it (free text, "
                                                   "e.g. `c. 1889`).")
    image_url: str = Field(description="Server-absolute path of the display-sized JPEG (resolve against the "
                                       "server base URL). Not token-protected, like the rest of the media.")
    thumb_url: str = Field(description="Server-absolute path of the thumbnail.")
    is_personal: bool = Field(description="True for the owner's own photos (My Photos), not museum art.")


class Display(BaseModel):
    id: str = Field(description="The display's id (what you put in the URL).")
    name: str = Field(description="Friendly name for UI. Pieria has no separate display-name setting "
                                  "(the appliance's configured name IS its display id), so this equals "
                                  "`id` today; show it, but address the display by `id`.")
    live: bool = Field(description="Checking in right now (heartbeat within ~15 s). Only live displays "
                                   "accept commands.")
    last_seen: datetime = Field(description="Last check-in (UTC).")
    kind: Literal["canvas", "eink", "frame", "unknown"] = Field(
        description="canvas = browser/kiosk display; eink = pull-on-wake e-ink panel; frame = Samsung "
                    "Frame TV push target; unknown = not derivable yet (a display is stamped the next "
                    "time it checks in).")
    playlist: Optional[str] = Field(None, description="Name of the playlist it is playing "
                                                      "(the same string `set_playlist` takes).")
    playlist_id: Optional[int] = Field(None, description="Id of that playlist (what `set_playlist` also "
                                                         "accepts); null if unknown or since renamed.")
    updated_at: datetime = Field(description="UTC. When this display's now-playing, paused flag or mode "
                                             "last changed (falls back to `last_seen` before the first "
                                             "change) — compare it to tell a queued command has taken effect.")
    mode: Optional[RenderMode] = Field(None, description="Render mode, canvas displays only. Best effort: "
                                                         "the last mode commanded through this API, else the "
                                                         "playlist's default mode.")
    artwork: Optional[ArtworkCompact] = Field(None, description="What it is showing (null before its first "
                                                                 "frame). A read — never advances playback.")
    paused: bool = Field(False, description="Server-side pause flag. While true, the display's own "
                                            "auto-advance (and an e-ink pull) re-serves the current "
                                            "artwork; an explicit `next`/`previous` still advances once.")


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: CommandAction
    playlist: Optional[str] = Field(None, description="`set_playlist`: a playlist name from "
                                                      "`GET /playlists`. Give this OR `playlist_id`.")
    playlist_id: Optional[int] = Field(None, ge=1, le=MAX_ID,
                                       description="`set_playlist`: a playlist id from `GET /playlists`. "
                                                   "Give this OR `playlist`.")
    mode: Optional[RenderMode] = Field(None, description="Required for `set_mode`.")

    @model_validator(mode="after")
    def _needs(self):
        if self.action is CommandAction.set_playlist:
            named = bool((self.playlist or "").strip())
            if named == (self.playlist_id is not None):
                raise ValueError("set_playlist needs exactly one of `playlist` (name) or `playlist_id`")
        if self.action is CommandAction.set_mode and self.mode is None:
            raise ValueError("`mode` is required for set_mode")
        return self


class CommandAccepted(BaseModel):
    status: Literal["queued"] = "queued"


class ShowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artwork_id: int = Field(ge=1, le=MAX_ID, description="An approved artwork id (see `GET /artworks/{id}`, `GET /search`).")


class QuietRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["on", "off", "auto"] = Field(
        description="on = force quiet now. off = force awake now. auto = clear any override; the "
                    "quiet-hours schedule rules again.")
    until: Optional[datetime] = Field(
        None, description="ISO-8601 instant (a value with no offset is read as UTC); must be in the "
                          "future. For on/off the override ends at `until` if given, else at the NEXT "
                          "scheduled quiet boundary (whichever is first) — so a forced state never sticks "
                          "silently; with no quiet schedule configured it lasts until changed. Ignored "
                          "for auto.")


class QuietState(BaseModel):
    active: bool = Field(description="Quiet (panel off/blank) right now, whatever the cause.")
    mode: Literal["on", "off", "auto"] = Field(
        description="The override in force: on / off, or auto when none (the schedule rules).")
    source: Literal["schedule", "manual", "none"] = Field(
        description="manual = an override from `POST /quiet` is in force (it may force quiet OFF too); "
                    "schedule = the quiet-hours schedule is making it quiet; none = not quiet.")
    until: Optional[datetime] = Field(
        None, description="UTC. When this state ends: the override's expiry (null = indefinite), or the "
                          "end of the current scheduled quiet window. null when source is `none`.")


class PlaylistSummary(BaseModel):
    id: int
    name: str
    artwork_count: int = Field(description="Approved artworks in it.")
    display_time: int = Field(description="Seconds each artwork stays up.")
    shuffle: bool
    default_mode: str
    is_personal: bool = Field(description="A My Photos album rather than a museum collection.")


class ArtworkPage(BaseModel):
    total: int = Field(description="Total approved artworks in the playlist (ignoring limit/offset).")
    limit: int
    offset: int
    items: list[ArtworkCompact]


class License(BaseModel):
    id: Optional[str] = Field(None, description="Licence id, e.g. `CC-BY-4.0`, `CC0-1.0`, `PD`; null if unknown.")
    name: Optional[str] = Field(None, description="Display name, e.g. `CC BY 4.0`.")
    url: Optional[str] = Field(None, description="Licence deed URL (http/https only).")
    attribution: Optional[str] = Field(None, description="Credit line to show exactly as given.")
    attribution_url: Optional[str] = Field(None, description="Source page for the credit (http/https only).")
    origin_url: Optional[str] = Field(None, description="The work's real web source (http/https only).")
    requires_attribution: bool = Field(description="True when the licence obliges you to show the credit "
                                                   "(CC BY). Public-domain/CC0 works: false.")


class ArtworkDetail(ArtworkCompact):
    series: Optional[str] = None
    placard: Optional[str] = Field(None, description="The placard text (plain, Markdown stripped).")
    artist_role: Optional[str] = None
    creation_date: Optional[str] = None
    culture: Optional[str] = Field(None, description="Cultural context / movement / period.")
    medium: Optional[str] = None
    tags: list[str] = []
    resolution_tier: Optional[str] = Field(None, description='"HD" | "4K" | "8K" when known.')
    license: License


class SearchResults(BaseModel):
    query: str
    total: int = Field(description="Matches in the library (ignoring limit).")
    items: list[ArtworkCompact] = Field(description="Best matches first (title/artist hits rank above "
                                                    "placard-text hits).")


class Schedule(BaseModel):
    """Night & quiet-hours schedule. Times are `HH:MM` 24h in the server's local time."""
    enabled: bool
    day_brightness: float = Field(description="0.1 - 1.0")
    night_brightness: float = Field(description="0.1 - 1.0")
    night_warmth: float = Field(description="0.0 - 1.0 amber tint at full night")
    evening_start: str
    night_start: str
    morning_start: str
    day_start: str
    quiet_enabled: bool
    quiet_start: str
    quiet_end: str
    quiet_mode: Literal["cec", "blackout"]


class SchedulePatch(BaseModel):
    """Any subset of the schedule; omitted fields keep their value."""
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    day_brightness: Optional[float] = None
    night_brightness: Optional[float] = None
    night_warmth: Optional[float] = None
    evening_start: Optional[str] = None
    night_start: Optional[str] = None
    morning_start: Optional[str] = None
    day_start: Optional[str] = None
    quiet_enabled: Optional[bool] = None
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    quiet_mode: Optional[Literal["cec", "blackout"]] = None


class ScheduleState(BaseModel):
    """What the display should look like right now."""
    enabled: bool
    brightness: float = Field(description="0.1 - 1.0 effective screen brightness now.")
    warmth: float = Field(description="0.0 - 1.0 effective amber tint now.")
    quiet: bool = Field(description="Panel should be off/blank: inside quiet hours, or forced by a manual "
                                   "override (`POST /quiet`).")
    quiet_mode: Literal["cec", "blackout"]


# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------

def _compact(a: ArtworkModel) -> ArtworkCompact:
    return ArtworkCompact(id=a.id, title=a.title, artist=a.agent_name,
                          year=a.date_display or a.creation_date,
                          image_url=f"/artworks/{a.id}/display.jpg",
                          thumb_url=f"/artworks/{a.id}/thumbnail", is_personal=bool(a.is_personal))


def _server_id(db: Session) -> str:
    """Stable UUID, minted once into the Settings KV (the unique key makes concurrent first calls on
    different workers converge on one value)."""
    for _ in range(2):
        row = db.query(SettingsModel).filter(SettingsModel.setting_key == SERVER_ID_KEY).first()
        if row and row.setting_value:
            return row.setting_value
        try:
            _upsert_setting(db, SERVER_ID_KEY, str(uuid.uuid4()))
            db.commit()
        except IntegrityError:
            db.rollback()
    raise ApiError(500, "internal_error", "could not mint server id")


def _setting(db: Session, key: str) -> Optional[str]:
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == key).first()
    return row.setting_value if row else None


def _frame_id(db: Session) -> str:
    return (_setting(db, "frame_display_id") or "").strip() or "frame-tv"


def _display_kind(row: ActiveDisplayModel, frame_id: str) -> str:
    if row.display_id == frame_id:
        return "frame"
    return row.kind if row.kind in ("canvas", "eink") else "unknown"


def _display_view(db: Session, row: ActiveDisplayModel, live: bool, frame_id: str) -> Display:
    kind = _display_kind(row, frame_id)
    mode = None
    if kind == "canvas":
        mode = _setting(db, DISPLAY_MODE_PREFIX + row.display_id)
        if mode is None and row.current_playlist:
            pl = db.query(PlaylistModel).filter(PlaylistModel.name == row.current_playlist).first()
            mode = pl.default_mode if pl else None
        if mode not in {m.value for m in RenderMode}:
            mode = None
    art = (db.query(ArtworkModel).filter(ArtworkModel.id == row.current_artwork_id).first()
           if row.current_artwork_id else None)
    pl = (db.query(PlaylistModel.id).filter(PlaylistModel.name == row.current_playlist).first()
          if row.current_playlist else None)
    last_seen = _as_utc(row.last_seen_at)
    return Display(id=row.display_id, name=row.display_id, live=live, last_seen=last_seen, kind=kind,
                   playlist=row.current_playlist, playlist_id=pl[0] if pl else None,
                   updated_at=display_updated_at(db, row.display_id) or last_seen,
                   mode=mode, artwork=_compact(art) if art else None,
                   paused=is_display_paused(db, row.display_id))


def _visible_playlist(db: Session, playlist_id: int) -> PlaylistModel:
    p = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first()
    if p is None or p.name.startswith("_"):   # "_"-prefixed = internal pseudo-collections, hidden like /playlists
        raise ApiError(404, "not_found", "no such playlist")
    return p


def _like(token: str) -> str:
    esc = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


_SEARCH_FIELDS = (ArtworkModel.title, ArtworkModel.agent_name, ArtworkModel.cultural_context,
                  ArtworkModel.series, ArtworkModel.medium, ArtworkModel.tags,
                  ArtworkModel.description_narrative)

_RESP = {401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token "
                                                       "(`WWW-Authenticate: Bearer`)."},
         403: {"model": ErrorResponse, "description": "Token lacks the required scope."}}

router = APIRouter(responses=_RESP)


# ---------------------------------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------------------------------

@router.get("/info", response_model=Info, tags=["server"], summary="Server identity and version",
            description="Also what to feature-detect on (`api_features`) and what the calling token may do "
                        "(`token_scopes`).")
def get_info(token: ApiTokenModel = Depends(require_read), db: Session = Depends(get_db)):
    display_name = None
    if config.IS_APPLIANCE:
        conf = host_health.read_conf() or {}
        display_name = (conf.get("values") or {}).get("DISPLAY_ID") or None
    return Info(name="Pieria", version=config.APP_VERSION, api_version=API_VERSION,
                appliance_mode=config.IS_APPLIANCE, server_id=_server_id(db), display_name=display_name,
                api_features=API_FEATURES, token_scopes=api_tokens.token_scopes(token))


@router.get("/displays", response_model=list[Display], tags=["displays"],
            summary="Displays this server knows, with what each is showing",
            dependencies=[Depends(require_read)])
def list_displays(db: Session = Depends(get_db)):
    """Includes sleeping e-ink panels (`live: false`). Pure read — never advances playback."""
    frame_id = _frame_id(db)
    return [_display_view(db, row, live, frame_id) for row, live in known_displays(db)]


@router.get("/displays/{display_id}", response_model=Display, tags=["displays"],
            summary="One display", dependencies=[Depends(require_read)],
            responses={404: {"model": ErrorResponse, "description": "Unknown display."}})
def get_display(display_id: str, db: Session = Depends(get_db)):
    row = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
    if row is None:
        raise ApiError(404, "not_found", "no such display")
    frame_id = _frame_id(db)
    return _display_view(db, row, _is_live(row, datetime.now(UTC)), frame_id)


_ACTION_TO_INTERNAL = {CommandAction.next: "next_image", CommandAction.previous: "prev_image",
                       CommandAction.show_placard: "show_placard",
                       CommandAction.set_playlist: "set_playlist", CommandAction.set_mode: "set_mode"}


@router.post("/displays/{display_id}/commands", response_model=CommandAccepted, status_code=202,
             tags=["displays"], summary="Send a command to a live display",
             dependencies=[Depends(require_control)],
             responses={404: {"model": ErrorResponse, "description": "Unknown display."},
                        409: {"model": ErrorResponse,
                              "description": "`not_live`: the display is not checking in, so the command "
                                             "was NOT queued (it would never be delivered). "
                                             "`unsupported_for_kind`: a canvas-only action "
                                             "(next/previous/show_placard/set_playlist/set_mode) sent to "
                                             "an e-ink or Frame display, which has no live channel."},
                        422: {"model": ErrorResponse,
                              "description": "Unknown action/mode, missing field, or unknown playlist."}})
def post_command(display_id: str, body: CommandRequest, db: Session = Depends(get_db)):
    """Queued for the display's WebSocket relay (~1 s). 202 means queued, not yet shown.

    `pause` / `resume` set a server-side flag first, so they also succeed (202) on a sleeping e-ink or
    Frame display, which honours the flag on its next pull; a Canvas additionally gets the command
    relayed so it stops/starts its own timer. A display of `unknown` kind (not stamped yet) is treated
    as a Canvas: it needs to be live."""
    row = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
    if row is None:
        raise ApiError(404, "not_found", "no such display")
    if body.action in (CommandAction.pause, CommandAction.resume):
        pull_based = _display_kind(row, _frame_id(db)) in ("eink", "frame")
        if not pull_based and not _is_live(row, datetime.now(UTC)):
            raise ApiError(409, "not_live", "display is not live (not checking in); command not queued")
        set_display_paused(db, display_id, body.action is CommandAction.pause)
        if not pull_based:
            queue_remote_command(db, display_id, body.action.value)
        return CommandAccepted()
    if _display_kind(row, _frame_id(db)) in ("eink", "frame"):
        raise ApiError(409, "unsupported_for_kind",
                       f"{body.action.value!r} needs a live Canvas display; this display is pull-based "
                       "(only pause, resume and show apply)")
    playlist = None
    if body.action is CommandAction.set_playlist:
        if body.playlist_id is not None:
            pl = db.query(PlaylistModel).filter(PlaylistModel.id == body.playlist_id).first()
        else:
            pl = db.query(PlaylistModel).filter(PlaylistModel.name == body.playlist.strip()).first()
        if pl is None or pl.name.startswith("_"):
            raise ApiError(422, "unknown_playlist", "no such playlist")
        playlist = pl.name
    if not _is_live(row, datetime.now(UTC)):
        raise ApiError(409, "not_live", "display is not live (not checking in); command not queued")
    mode = body.mode.value if body.action is CommandAction.set_mode else None
    queue_remote_command(db, display_id, _ACTION_TO_INTERNAL[body.action], playlist, mode)
    if mode:
        _upsert_setting(db, DISPLAY_MODE_PREFIX + display_id, mode)
        touch_display_updated(db, display_id)
        db.commit()
    return CommandAccepted()


@router.post("/displays/{display_id}/show", response_model=CommandAccepted, status_code=202,
             tags=["displays"], summary="Show a specific artwork now",
             dependencies=[Depends(require_control)],
             responses={404: {"model": ErrorResponse, "description": "Unknown display, or unknown / "
                                                                       "unapproved artwork."},
                        409: {"model": ErrorResponse,
                              "description": "`not_live`: a Canvas display that is not checking in "
                                             "(nothing queued)."}})
def post_show(display_id: str, body: ShowRequest, db: Session = Depends(get_db)):
    """Canvas: shown within ~1 s, then the rotation continues from it. E-ink / Frame: becomes the next
    item served on the display's next pull (it waits, even while the display sleeps, for up to 6 h).
    A paused display stays paused after showing it."""
    row = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
    if row is None:
        raise ApiError(404, "not_found", "no such display")
    if db.query(ArtworkModel.id).filter(ArtworkModel.id == body.artwork_id,
                                        ArtworkModel.status == "approved").first() is None:
        raise ApiError(404, "not_found", "no such artwork")
    if _display_kind(row, _frame_id(db)) in ("eink", "frame"):
        set_pending_show(db, display_id, body.artwork_id)
        return CommandAccepted()
    if not _is_live(row, datetime.now(UTC)):
        raise ApiError(409, "not_live", "display is not live (not checking in); command not queued")
    queue_remote_command(db, display_id, "show_artwork", artwork_id=body.artwork_id)
    return CommandAccepted()


@router.get("/playlists", response_model=list[PlaylistSummary], tags=["library"],
            summary="Playlists (light — no artworks embedded)", dependencies=[Depends(require_read)])
def list_playlists(db: Session = Depends(get_db)):
    counts = dict(db.query(playlist_artwork.c.playlist_id, func.count())
                  .join(ArtworkModel, ArtworkModel.id == playlist_artwork.c.artwork_id)
                  .filter(ArtworkModel.status == "approved")
                  .group_by(playlist_artwork.c.playlist_id).all())
    return [PlaylistSummary(id=p.id, name=p.name, artwork_count=counts.get(p.id, 0),
                            display_time=p.display_time, shuffle=bool(p.shuffle),
                            default_mode=p.default_mode, is_personal=bool(p.is_personal))
            for p in db.query(PlaylistModel).order_by(PlaylistModel.id).all()
            if not p.name.startswith("_")]


@router.get("/playlists/{playlist_id}/artworks", response_model=ArtworkPage, tags=["library"],
            summary="Artworks in a playlist, in playlist order", dependencies=[Depends(require_read)],
            responses={404: {"model": ErrorResponse, "description": "Unknown playlist."}})
def list_playlist_artworks(playlist_id: int = IdPath(), limit: int = Query(50, ge=1, le=200),
                           offset: int = Query(0, ge=0, le=MAX_OFFSET),
                           db: Session = Depends(get_db)):
    p = _visible_playlist(db, playlist_id)
    base = (db.query(ArtworkModel).join(playlist_artwork, playlist_artwork.c.artwork_id == ArtworkModel.id)
            .filter(playlist_artwork.c.playlist_id == p.id, ArtworkModel.status == "approved"))
    total = base.count()
    rows = (base.order_by(playlist_artwork.c.display_order, ArtworkModel.id).offset(offset).limit(limit).all())
    return ArtworkPage(total=total, limit=limit, offset=offset, items=[_compact(a) for a in rows])


@router.get("/artworks/{artwork_id}", response_model=ArtworkDetail, tags=["library"],
            summary="One artwork: placard, metadata, licence and credit",
            dependencies=[Depends(require_read)],
            responses={404: {"model": ErrorResponse, "description": "Unknown (or unapproved) artwork."}})
def get_artwork(artwork_id: int = IdPath(), db: Session = Depends(get_db)):
    from config import strip_markdown
    a = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id,
                                      ArtworkModel.status == "approved").first()
    if a is None:
        raise ApiError(404, "not_found", "no such artwork")
    meta = placard_metadata(a)
    placard = strip_markdown(meta["description"]) if meta["description"] else None
    title = strip_markdown(meta["title"]) if meta["title"] else None
    series = strip_markdown(meta["series"]) if meta["series"] else None
    tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    base = _compact(a).model_dump()
    base["title"] = title
    return ArtworkDetail(
        **base, series=series, placard=placard, artist_role=a.agent_role,
        creation_date=a.creation_date, culture=a.cultural_context, medium=a.medium, tags=tags,
        resolution_tier=a.resolution_tier,
        license=License(id=meta["license"], name=meta["license_name"],
                        url=core_licensing.safe_http_url(meta["license_url"]),
                        attribution=meta["attribution"],
                        attribution_url=core_licensing.safe_http_url(meta["attribution_url"]),
                        origin_url=core_licensing.safe_http_url(meta["origin_url"]),
                        requires_attribution=meta["requires_attribution"]))


@router.get("/search", response_model=SearchResults, tags=["library"],
            summary="Text search over the approved library", dependencies=[Depends(require_read)])
def search(q: str = Query(..., min_length=1, max_length=200,
                          description="Whitespace-separated terms; ALL must match (case-insensitive) in "
                                      "title, artist, movement/period, series, medium, tags or placard."),
           limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    tokens = [t for t in q.lower().split() if t][:8]
    if not tokens:
        raise ApiError(422, "validation_error", "q must contain a search term")
    base = db.query(ArtworkModel).filter(ArtworkModel.status == "approved")
    rank = 0
    for t in tokens:
        pat = _like(t)
        base = base.filter(or_(*[f.ilike(pat, escape="\\") for f in _SEARCH_FIELDS]))
        rank = rank + case((ArtworkModel.title.ilike(pat, escape="\\"), 2), else_=0) \
                    + case((ArtworkModel.agent_name.ilike(pat, escape="\\"), 2), else_=0)
    total = base.count()
    rows = base.order_by(rank.desc(), ArtworkModel.id).limit(limit).all()
    return SearchResults(query=q, total=total, items=[_compact(a) for a in rows])


@router.get("/schedule", response_model=Schedule, tags=["schedule"],
            summary="Night & quiet-hours schedule", dependencies=[Depends(require_read)])
def get_schedule(db: Session = Depends(get_db)):
    return _load_schedule(db)


@router.patch("/schedule", response_model=Schedule, tags=["schedule"],
              summary="Change part of the schedule", dependencies=[Depends(require_control)],
              responses={422: {"model": ErrorResponse, "description": "A value is out of range or malformed."}})
def patch_schedule(body: SchedulePatch, db: Session = Depends(get_db)):
    try:
        return apply_schedule_patch(db, body.model_dump(exclude_none=True))
    except ScheduleError as e:
        raise ApiError(422, "validation_error", str(e))


@router.get("/schedule/state", response_model=ScheduleState, tags=["schedule"],
            summary="Brightness/warmth/quiet resolved for right now", dependencies=[Depends(require_read)])
def get_schedule_state(db: Session = Depends(get_db)):
    return resolve_schedule_state(_load_schedule(db), datetime.now(), get_quiet_override(db))


def _quiet_view(db: Session) -> QuietState:
    return QuietState(**quiet_status(_load_schedule(db), get_quiet_override(db)))


@router.get("/quiet", response_model=QuietState, tags=["schedule"],
            summary="Is the display quiet right now, and why", dependencies=[Depends(require_read)])
def get_quiet(db: Session = Depends(get_db)):
    """The effective quiet state: a manual override if one is in force, else the quiet-hours schedule.
    An expired override is cleared on read."""
    return _quiet_view(db)


@router.post("/quiet", response_model=QuietState, tags=["schedule"],
             summary="Manually force quiet on or off", dependencies=[Depends(require_control)],
             responses={422: {"model": ErrorResponse, "description": "`until` is in the past."}})
def post_quiet(body: QuietRequest, db: Session = Depends(get_db)):
    """`mode:on` blanks the display now (Canvas blackout; the appliance also powers the panel off when
    quiet mode is `cec`); `mode:off` forces it awake; `mode:auto` clears the override. on/off end at
    `until` or the next scheduled quiet boundary, whichever is first. Takes effect on the display's next
    schedule poll (~60 s for the Canvas). Returns the resulting state, same as `GET /quiet`."""
    until = body.until if body.mode != "auto" else None
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        try:
            until = until.astimezone(UTC)
        except (OverflowError, ValueError):   # e.g. 9999-12-31T23:59:59-12:00 has no UTC representation
            raise ApiError(422, "validation_error", "until is out of range")
        if until <= datetime.now(UTC):
            raise ApiError(422, "validation_error", "until must be in the future")
    set_quiet_override(db, _load_schedule(db), {"on": True, "off": False, "auto": None}[body.mode], until)
    return _quiet_view(db)


# ---------------------------------------------------------------------------------------------------
# The sub-app (mounted at /api/v1 by app.py)
# ---------------------------------------------------------------------------------------------------

def build_v1_app() -> FastAPI:
    # Mirror the main app's demo decision: demo mode registers no introspection surface at all (the
    # demo gate in core/demo.py also refuses /api/v1/* outright — it is not on the allowlist).
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if config.DEMO_MODE else {
        "docs_url": "/docs", "redoc_url": None, "openapi_url": "/openapi.json"}
    sub = FastAPI(
        title="Pieria API", version="1",
        description=("Control and read your Pieria. Every request needs `Authorization: Bearer <token>` "
                     "(mint one in Admin -> API & Integrations). Within v1 changes are additive only: "
                     "clients must ignore unknown fields and tolerate new enum values."),
        **docs)
    sub.include_router(router)
    sub.add_exception_handler(ApiError, _api_error_handler)
    sub.add_exception_handler(StarletteHTTPException, _http_error_handler)
    sub.add_exception_handler(RequestValidationError, _validation_error_handler)
    sub.add_exception_handler(Exception, _unhandled_error_handler)
    return sub


v1_app = build_v1_app()
