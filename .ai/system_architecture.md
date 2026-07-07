# Screen Docent — System Architecture

> **Version:** 0.9.1 · **Last Updated:** 2026-06-27

---

## Thesis

**Own the curation brain; render beautifully onto whatever panel exists; make onboarding one step.**
A single self-hosted FastAPI server is the *brain* (sourcing, curation, AI enrichment, scheduling). It
drives **many output targets** — an animated browser/TV Canvas, a stateless e-ink image API, and a push
adapter for Samsung Frame TVs — and is growing a **federated catalog** so anyone can publish collections.

## Tech Stack

| Layer | Technology | Role |
|-------|-----------|------|
| **Runtime** | Python 3.12 | Application language |
| **Web Framework** | FastAPI 0.111 | ASGI backend, REST API, WebSocket hub |
| **ASGI Server** | Uvicorn 0.30 (4 workers) | Multi-process HTTP/WS serving |
| **Database** | SQLite 3 via SQLAlchemy 2.0 | Local, file-based relational store (`./data/artwork.db`) |
| **Migrations** | Alembic 1.13 | Versioned schema migrations (`migrations/versions/`) |
| **AI (BYO model)** | `ai_client.py` — one **OpenAI-compatible** client | Any provider (Gemini-compat, OpenAI, Anthropic, OpenRouter, local Ollama) = base_url + api_key + model. Configured in-GUI; `google-generativeai` **dropped**. |
| **RAG Context** | Wikipedia API (`wikipedia` 1.4) | Ground-truth context for the curator pipeline |
| **Image Processing** | Pillow 10.3 + pillow-heif 0.18 | Optimisation, EXIF-orient, crop, e-ink palette-quantize + Floyd–Steinberg dither; **HEIC/HEIF decode** (iPhone uploads, transcoded to JPEG on ingest). pillow-heif pinned to hold Pillow 10.3 (manylinux wheel bundles libheif → no apt) |
| **HTTP Client** | httpx 0.27 | Async museum/image/manifest fetches |
| **Crypto** | PyNaCl 1.6 (Ed25519) | Federation: signed-manifest verification (verified-publisher tier) |
| **Frame TV** | `samsungtvws` 3.0 | Push render adapter (LAN Art Mode, no Samsung account) |
| **Frontend** | Vanilla JS + CSS (GPU-accelerated) | Canvas display, admin, mobile remote — **no SPA framework** |
| **Vendored JS** | Cropper.js, Sortable.js, QRCode.js (in `static/vendor/`) | Local copies — **zero external runtime deps** (air-gap-safe) |
| **Containerisation** | Docker + Docker Compose | Self-contained image; `static/` + Python baked in (rebuild to deploy) |
| **Dev tooling** | Ruff (lint), pytest (+asyncio, respx), pre-commit (gitleaks + ruff) | **Dev-only** — never in the shipped image (requirements-dev.txt) |

---

## Architecture Overview — one brain, many outputs

```
                         ┌──────────── CURATION BRAIN (FastAPI / Uvicorn ×4) ────────────┐
                         │                                                               │
  museum APIs ─ scout.py ┤  Sourcing: 8 museum scouts + NASA + Wikimedia (live Discover) │
                         │  Enrichment: agents.py (vision) · curator.py (RAG+Wikipedia)  │
                         │  Search: query_classifier.py · result_ranker.py (+clean_title)│
                         │  Director: get_next_image() bag-shuffle + affinity weighting  │
                         │  Store: SQLAlchemy + Alembic → ./data/artwork.db              │
                         │  Media: ./Artwork/_Library (canonical), served via StaticFiles│
                         └───────────────┬───────────────┬───────────────┬──────────────┘
                                         │               │               │
                  WebSocket /ws/{id} +   │   GET /display/{id}/current.{png,bmp}   POST → Frame
                  /next-image            │   (epaper.py: crop+quantize+dither)     (frame_push.py)
                                         ▼               ▼               ▼
                              ┌──────── OUTPUT TARGETS ─────────────────────────┐
                              │  Canvas (TV/browser)   e-ink / BYOS frames   Samsung Frame Art Mode │
                              │  index.html + app.js   pull-on-wake image     (push, LAN, no sub)    │
                              └─────────────────────────────────────────────────┘

  Catalog / Federation: static/catalog/ (bundled) + subscribed Manifest v2 feeds (federation.py)
  → merged in /api/catalog, badged Official / Verified / Community.

  Volumes:  ./Artwork → /app/Artwork   ·   ./data → /app/data
```

### Output target 1 — The Canvas (TV / browser)
- **Route:** `/` → `static/index.html` + `static/app.js`. Zero-chrome full-screen viewer for Fire TV /
  Android TV / kiosk browsers.
- WebSocket `/ws/{display_id}` for targeted remote commands; auto-cycles via `/next-image`.
- Three render modes: **Ken Burns pan** (GPU, **focal-adaptive** — anchors the zoom/drift on the
  artwork's focal point via the Web Animations API so off-center subjects aren't panned out of frame),
  **static crop**, **contain + blurred matte**.
- **Resolution-capped image delivery (NOT full-res).** `/next-image` hands the Canvas
  `/artworks/{id}/display.jpg?v=<mtime>` — a derivative capped at **`DISPLAY_MAX_EDGE` = 7680 px**
  (8K) long edge, EXIF-baked, JPEG q90 (`render_canvas_image`). Museum originals run 40–110 MB / 150+ MP,
  which a Pi-class browser can't decode/GPU-texture (over the common 8192 `GL_MAX_TEXTURE_SIZE`) — so the
  placard would cycle while the image never painted. 7680 keeps ~4K detail after a portrait→landscape
  cover-crop + Ken Burns zoom while staying under that texture ceiling. **The full-res original is kept on
  disk untouched** (crop/focal quality unaffected; the cap is a one-line bump). Derivatives are
  **disk-cached** at `Artwork/_derivatives/{id}-{mtime}-7680.jpg` (atomic, mtime-keyed → self-busting on
  re-crop) and generated off the event loop via `run_in_threadpool`. They're **pre-warmed**: a leader-only
  boot sweep (`warm_all_canvas_cache`) renders every approved artwork, and the add paths fire a
  fire-and-forget warm — so the one-time multi-second encode never lands on the display path. (e-ink/Frame
  are unaffected — they render from the original via `get_next_image`.)
- **Museum placard** with metadata + a **QR code → `/art/{id}`** (server-hosted "Learn More" page,
  works offline; no Google hand-off).
- **Empty-state** overlay ("No art yet — add it at <host>/admin") instead of a black screen; a
  first-load "Manage at …/admin" hint.
- **Sleep defeater:** a hidden looping **local** `static/assets/keepawake.mp4` (vendored — was a
  w3schools URL) so it works offline.
- Hierarchical config: URL param → Playlist DB default → Global default.
- **Reboot resumes the last-played playlist.** With no `?playlist=`, the Canvas asks
  `GET /api/displays/{id}/preferred-playlist`, which resolves **last-played for this display →
  `default_playlist` setting → null** (then the Canvas falls back to first-non-empty). `/next-image`
  records `last_playlist:<display_id>`; the fallback is pinnable in Admin → Settings → **Default Playlist**.
  (Both stored in the Settings KV table — no migration.)

### Output target 2 — e-ink / BYOS frames (`epaper.py`)
- **Stateless pull-on-wake:** `GET /display/{id}/current.{png,bmp}?w=&h=&palette=`. Reuses
  `get_next_image()` selection, then EXIF-orient → RGB-normalise → **focal-anchored** cover/contain crop → contrast/sat
  pre-boost → palette quantize + Floyd–Steinberg dither. Palettes: `spectra6`/`acep7`/`gray4`/`gray16`.
  Returns `X-Refresh-After` as the sleep hint; upserts `active_displays`.

### Output target 3 — Samsung Frame TV push (`frame_push.py`)
- Renders the selected artwork to a full-colour TV-res JPEG (`epaper.render_fullcolor`) and uploads it
  into the Frame's Art Mode over the LAN (unofficial WebSocket via `samsungtvws`), leader-only loop.
  TV hidden behind a `FrameClient` ABC (`SamsungFrameClient` + `FakeFrameClient`) — all logic tested
  against the fake; real-hardware path in `integrations/frame-tv/`.

### Framing — per-artwork focal points
- Every artwork carries a normalized **`focal_x`/`focal_y`** (default `0.5,0.5` = center = prior behavior)
  — the visual subject the renderer keeps in frame. **One point feeds all three outputs:** the Canvas
  Ken Burns (transform-origin + background-position + focal-adaptive drift), and `epaper._fit_rgb`'s
  `ImageOps.fit` centering for e-ink + Frame.
- **Derived, baked once:** AI sets it during enrichment (`agents`/`curator`, shared
  `FOCAL_POINT_INSTRUCTION`/`apply_focal_point`); the bundled catalog + seed ship **pre-baked**
  `focal_point` (offline `tools/backfill_focal_*` — an in-IDE Claude-agent vision pass, no app API key);
  catalog/seed add + the seed loader copy it via `_focal_xy`. Settable per-artwork via
  `PATCH /artworks/{id}/crop` (which also persists the manual Cropper crop).

### The Admin & Mobile Remote
- **Admin** `/admin` (`admin.html`+`admin.js`): library/playlist CRUD, a full-screen **Edit** landing
  (crop + focal + placard + Re-enrich), and **Museum Art** — one search box (with catalog **autocomplete**
  via `<datalist>` ← `/api/catalog/suggest`) over a **Curated / Live** source toggle. Curated = browse/
  flat-search the bundled 524, add straight to library; thin/empty results surface a prominent **live-search
  escalation** of the same query. Live = 8 museum scouts + NASA + Wikimedia, finalized via **inline review**
  (the discovery card expands in place into the review form — enrichment streams in, Approve/Discard right
  there; no Review-Queue hop). **Bulk ☑ Select** across Library/Collections (add/remove/delete), the
  **Review Queue** (Approve & Publish a batch), and the curated grid (Add selected). **Review Queue**
  (live-enriching) remains the batch catch-all. **Settings** (📡 This Server · 🧠 AI Engine BYO-model ·
  🖼️ Frame TV · 🌐 Subscriptions · 📚 Catalog Source · premium museum keys · Maintenance). **Responsive**
  (slide-in drawer under 768px). Themed toast/modal pattern (no native `alert/confirm/prompt`).
- **Remote** `/remote` (`remote.html`): mobile PWA; targets specific Canvas displays via
  `active_displays` + `remote_commands` (cross-worker, see below).

### Studio — "My Photos" (personal photos)
- **`/studio`** (`studio.html`): a phone-first front door for a user's OWN photos — multi-upload (+camera
  capture), optional **AI caption** (evocative photo-album voice; `is_local_base_url()` gives an honest
  on-device-vs-cloud privacy note), and **tap-to-set focal point**.
- **`POST /upload/personal`:** local-only, EXIF-oriented, **HEIC→JPEG transcoded** (iPhone default
  format; browsers can't render HEIC), `is_personal=True`, `status=approved` — it **deliberately skips
  the museum AI pipeline** (the photo is never sent to a model — the privacy headline) and the Review
  Queue; auto-files into a "My Photos" playlist. (The museum `POST /upload` also transcodes HEIC→JPEG
  while leaving other formats byte-identical.) `is_personal` also drives a
  **jargon-free placard** (caption + date only, no QR) on the Canvas and `/art/{id}`. Caption/date saved
  via `PATCH /api/studio/photo/{id}` (personal-only); a remote `catalog_url` is configurable separately.

### Multi-Worker Concurrency Model
Uvicorn runs 4 workers (separate memory). Cross-worker coordination via SQLite:
- **`active_displays`** — per-connection heartbeat upserts; `/api/remote/displays` lists recent.
- **`remote_commands`** — remote enqueues a targeted command; the owning worker's `command_poller()`
  delivers locally then deletes the row.
- **`display_playback_sessions`** — per-display bag-shuffle state survives reconnects.
- **Leader-only boot** (`fcntl` lock): one worker runs migrations, factory seed, and the Frame push loop.

---

## Catalog & Federation

- **Bundled catalog:** `static/catalog/` — a split manifest (`index.json` + per-collection files,
  524 served PD items / 24 collections, each carrying a baked **`focal_point`**) built offline by `tools/`
  (`catalog_spec.py` → `sources.py` resolvers → `build_catalog.py`, display-gated to ≥2000px). Loaded
  **from disk** — `origin: bundled`. A remote **`catalog_url`** (Settings → 📚 Catalog Source) can
  override it with a static-hosted manifest, falling back to bundled on any fetch failure.
- **Federation (`federation.py` + Manifest v2):** subscribe to a third-party collection by URL.
  - **`docs/manifest-v2.md`** is the spec; **`tools/manifest_validator.py`** the executable validator
    (pure-Python, strict on required/types/**per-asset licensing**, tolerant of unknown fields →
    forward-compatible with optional `interpretation` + `image.focal_point` assets).
  - **Safety (we index pointers, never host bytes):** SSRF guard (blocks private/loopback/link-local
    IPs), redirects disabled, size cap + content-type/JSON check + timeout, strict validation, a
    host/publisher blocklist. Untrusted strings are escaped at render time.
  - **Trust:** Ed25519 signed manifests (`canonical_bytes`/`verify_signature`/`assess_trust`).
    `verified` = signed **and** publisher key in the curated `registry/trusted_publishers.json`;
    else `community` (unsigned, or validly self-signed but not registry-trusted = TOFU). A
    present-but-invalid signature is **rejected** outright. `tools/sign_manifest.py` = publisher CLI.
  - **Provenance is OUR record:** the `subscriptions` table (`SubscriptionModel`) stamps origin/
    publisher/trust at subscribe-time — never trusted from the manifest body. Subscribed collections
    merge into `/api/catalog` namespaced `sub_<id>`, badged **Official / Verified / Community** in the
    admin. A third-party feed is structurally incapable of masquerading as bundled (disk vs. fetch path).
- **Shared downloader:** seed / discovery / catalog / federation-add all route through
  `_download_image_to_library` (descriptive UA, 429 retry, redirect, collision-safe name, image
  validation); federation-add additionally SSRF-guards the third-party image URL.

---

## File Tree (Core Application)

```
Screen-Docent/
├── app.py              # FastAPI: routes, WS hub, middleware, lifespan, catalog+federation+e-ink endpoints
├── ai_client.py        # ONE OpenAI-compatible client for all model calls (BYO provider/key/model)
├── config.py           # Shared constants (ARTWORK_ROOT, LIBRARY_DIR) — breaks circular imports
├── database.py         # SQLAlchemy engine + session factory (schema owned by Alembic, NOT create_all)
├── db_migrate.py       # Boot schema mgmt: run_migrations() — build/reconcile to head, fail loud (ADR-035)
├── models.py           # ORM: Playlist, Artwork, DiscoveryQueue, Settings, ActiveDisplay,
│                       #   RemoteCommand, DisplayPlaybackSession, Subscription
├── agents.py           # Vision agent: image → VRA metadata (via ai_client)
├── curator.py          # RAG curator: Wikipedia → re-enrichment (via ai_client)
├── scout.py            # Museum scouts (Chicago/Met/Cleveland/Rijks/SMK + Harvard/Smithsonian/
│                       #   Europeana key-gated) + NASA + Wikimedia; shared PD/resolution helpers
├── query_classifier.py # Hybrid intent classifier
├── result_ranker.py    # Scoring + dedup + clean_title (museum-title normalization)
├── epaper.py           # e-ink render: crop + palette quantize + dither; render_fullcolor (Frame)
├── frame_push.py       # Samsung Frame push adapter (FrameClient ABC + fake + samsungtvws)
├── federation.py       # Manifest v2 fetch (SSRF/size/type guards), Ed25519 trust, sync, mapping
│
├── tools/              # OFFLINE/dev (runtime-independent)
│   ├── catalog_spec.py · sources.py · build_catalog.py   # bundled catalog builder
│   ├── backfill_focal_fetch.py · backfill_focal_bake.py  # focal-point backfill (in-IDE agent vision)
│   ├── manifest_validator.py   # Manifest v2 validator (also imported at runtime by federation)
│   ├── sign_manifest.py        # publisher keygen + sign CLI
│   └── make_gif.py
├── registry/trusted_publishers.json  # curated verified-publisher Ed25519 keys (federation)
├── docs/manifest-v2.md           # Manifest v2 spec
│
├── migrations/versions/  # Alembic revisions — single baseline 0001_baseline (squash, ADR-035)
├── static/
│   ├── index.html · app.js · styles.css     # Canvas + focal-adaptive Ken Burns + /art QR
│   ├── admin.html · admin.js                # admin (responsive, toast/modal, subs panel, badges)
│   ├── studio.html                          # "My Photos" personal-photo front door (/studio)
│   ├── remote.html · help.html · logo.svg · factory_seed.json
│   ├── catalog/            # bundled split manifest (index.json + per-collection)
│   ├── vendor/             # Cropper / Sortable / QRCode (vendored, offline)
│   └── assets/keepawake.mp4 # local sleep-defeater
├── integrations/          # MMM-ScreenDocent (MagicMirror²) · frame-tv (push_once CLI)
│
├── Artwork/_Library/      # canonical image store      ·  data/artwork.db  (volume-mapped)
│   └── Artwork/_derivatives/  # resolution-capped Canvas display cache (regenerable; internal _-dir)
├── Dockerfile · docker-compose.yml · requirements.txt · requirements-dev.txt
├── pyproject.toml (Ruff) · .pre-commit-config.yaml
├── tests/                 # pytest (253): scouts, resolvers, catalog (+search/suggest/bulk-add), epaper,
│                          #   frame_push, ai_client, download, ranker, detail_page, manifest_validator,
│                          #   federation, signing, personal (Studio +HEIC), bulk (approve/link/delete), focal
└── .ai/                   # dev context (system_architecture.md tracked; active_context + decision_log local)
```

---

## Core Development Rules

> [!CAUTION]
> Guardrails derived from the codebase + past decisions. Violating them introduces regressions.

1. **No heavy frontend frameworks.** Vanilla JS/CSS/HTML only — the Canvas must run on Fire-TV-class RAM.
2. **Serve media via `StaticFiles`** (`/media`), never `FileResponse` (event-loop block → the Phase-5 TTFB bug).
3. **Volume-map `./data` (the directory)**, not the `.db` file (SQLite WAL/inode conflicts).
4. **Preserve the hierarchical config override:** URL param → Playlist default → Global default.
5. **AI calls go through `ai_client.py`** (one OpenAI-compatible path). Do NOT reintroduce
   `google-generativeai` or hardcode a provider/model — config lives in `SettingsModel` (GUI-set, env fallback).
6. **AI pipelines run as background tasks** that create + close their own `SessionLocal()`.
7. **Optimise images before AI** (≤2048px, JPEG 85%, LANCZOS).
8. **Alembic is the single source of truth for the schema** (ADR-035). Add a migration on top of
   `0001_baseline` (additive — SQLite can't drop/rename in place); boot `db_migrate.run_migrations()`
   builds/reconciles to head and **halts loudly** on failure. Do NOT reintroduce `create_all` at boot
   (it masked missing migrations and caused the drift). Tests build schema via `create_all` on throwaway engines.
9. **WebSocket commands are targeted by `display_id` across workers via the DB** (`remote_commands` +
   `active_displays`); never assume in-process state is shared.
10. **All artwork lives in `Artwork/_Library/`** (canonical); playlist dirs are symlink/organisation only.
    **Every other dir directly under `Artwork/` is treated as a collection** by `sync_db_with_filesystem`
    (it mints a playlist and absorbs `.jpg`s into the library). Internal caches MUST be **underscore-prefixed**
    (`_Library`, `_derivatives`) — the enumeration guards skip `name.startswith("_")`. Never put a non-collection
    dir under `Artwork/` without the `_` prefix (this bit us: the `_derivatives` cache got absorbed as 97 bogus artworks).
11. **One robust downloader.** New image-download paths use `_download_image_to_library` (UA + 429 retry +
    validation) — don't hand-roll a bare `httpx.get` (default UA → Wikimedia/NASA 403).
12. **Rate-limit + progressive-fallback external APIs**; **log background-task errors** (`exc_info=True`).
13. **Never cache `/api/*`** (no-store); only static media + code assets are cached.
14. **Federation safety is non-negotiable.** Any path that fetches a third-party URL goes through
    `federation`'s SSRF guard + size/type caps + Manifest v2 validation; treat all manifest strings as
    untrusted (HTML-escape on render). We **index pointers, never host** third-party bytes.
15. **Trust is recorded by us, not claimed by the manifest.** Origin/trust live in `SubscriptionModel`;
    `verified` requires an Ed25519 signature whose key is in `registry/trusted_publishers.json`.
16. **No external runtime deps in the client.** Vendor JS/assets into `static/vendor/` + `static/assets/`
    (offline/air-gap). Ruff/pytest etc. are dev-only (requirements-dev.txt) — never the shipped image.
17. **`static/` + Python are baked into the image.** UI/backend changes need `docker compose build && up -d`;
    bump the `admin.js?v=`/`app.js?v=` cache-bust when changing those files.

---

## Admin Utilities & Key Endpoints

- **Factory Reset** `POST /api/admin/factory-reset` (JSON body `{"confirm":"RESET"}`, enforced
  server-side per ADR-036) — wipe non-seed data.
- **Discover maintenance** — clear pending / rejected history / orphaned approvals.
- **Catalog** — `GET /api/catalog` (index, origin-tagged) · `GET /api/catalog/{id}` · `GET /api/catalog/search`
  (flat AND-token) · `GET /api/catalog/suggest` (autocomplete) · `POST /api/catalog/add` · `POST /api/catalog/add-bulk`.
  *(search/suggest declared before `/{collection_id}` to avoid route-shadowing.)*
- **Review/library bulk** — `PATCH /artworks/{id}/approve` · `POST /artworks/approve-bulk` (pending→approved,
  status-guarded) · `PATCH /artworks/{id}/metadata` (edit approved in place) · `POST /artworks/delete` ·
  `POST`/`DELETE /playlists/{id}/artworks` (bulk link/unlink).
- **Federation** — `GET/POST/DELETE /api/subscriptions` · `POST /api/subscriptions/{id}/sync`.
- **Display image** — `GET /display/{id}/current.{png,bmp}` (e-ink). **Detail** — `GET /art/{id}`.
- **Studio** — `/studio` page · `POST /upload/personal` · `POST /api/studio/caption/{id}` ·
  `PATCH /api/studio/photo/{id}` · `PATCH /artworks/{id}/crop` (crop + focal point).
- **Settings** — `GET/POST /api/settings/ai` (+OAuth) · `/api/settings/frame` (+test) ·
  `/api/settings/catalog` (remote catalog source) · premium keys.
