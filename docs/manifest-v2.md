# Pieria — Manifest v2 (federation + interpretation)

> **Status:** schema frozen (draft 1, 2026-06-21). The validator
> (`tools/manifest_validator.py`) is the executable source of truth; this doc explains intent.
>
> **Want to publish a collection?** See [**How to publish**](how-to-publish.md) — author it in the
> Publisher Studio (`/publisher`) or the `tools/build_manifest` CLI. This page is the schema reference
> behind both.

Manifest v2 generalizes the v1 split catalog (`index.json` + per-collection files) into a
**federated** format any publisher can produce, and makes it **forward-compatible** with
optional future interpretation assets, without a breaking change.

## Why v2 differs from v1

v1 modeled an item as *image + placard*. v2 models an item as **an artwork plus a stack of
optional, independently-licensed assets**:

- **`image`** — the artwork raster (required). Has its own license/attribution.
- **`interpretation`** — OPTIONAL authored commentary: narrative sections, points-of-interest
  (regions + commentary the kiosk can pan/zoom to), and audio narration. **Authored and licensed
  separately from the image** (a PD image can carry copyrighted expert narration).

This is the key rights shift: **a license attaches to each asset, not to the item.** A creator
declares *what* they're licensing and *which part*. It also admits a new contributor type — the
interpretation author (art historian / docent / museum) who may contribute only commentary over a
public-domain image.

## Forward-compatibility rules (normative)

1. Every manifest carries `manifest_version` (currently `2`).
2. **Validators MUST ignore unknown fields** (object keys they don't recognize) rather than reject —
   so future optional fields can be added additively.
3. New *required* fields are only ever introduced under a bumped `manifest_version`.
4. The `interpretation` block is fully specified now but OPTIONAL and unpopulated in v2.0.

## Top-level manifest (one per collection)

| field | req | type | notes |
|---|---|---|---|
| `manifest_version` | ✓ | int | must be `2` |
| `id` | ✓ | string | collection id, unique within the publisher |
| `title` | ✓ | string | |
| `description` | | string | |
| `cover_image` | | string | URL of the image shown with the collection name in the browse view; defaults to the first item's thumbnail when omitted |
| `publisher` | | object | `{id, name, url?, public_key?}` — `public_key` is base64 Ed25519 |
| `generated_at` | | string | ISO-8601 |
| `default_license` | | string | applied to items that omit `image.license` |
| `items` | ✓ | array | see below |
| `signature` | | string | base64 Ed25519 over the canonical manifest (sorted keys, sans `signature`). Verified when present; required for "verified-publisher" tier. |

## Item

| field | req | type | notes |
|---|---|---|---|
| `id` | ✓ | string | stable within the publisher |
| `title` | ✓ | string | |
| `artist` | | string | |
| `artist_role` | | string | e.g. "Painter" |
| `date` | | string | display date, e.g. "1889" |
| `creation_date` | | string | |
| `medium` | | string | |
| `culture` | | string | cultural/movement context |
| `tags` | | array of string | |
| `placard` | | string | short narrative shown on the display placard |
| `image` | ✓ | object | the artwork asset (below) |
| `access` | | string | `free` (default) or `entitlement:<provider>:<id>` |
| `interpretation` | | object | OPTIONAL docent layer (below) |

### `image` asset

| field | req | type | notes |
|---|---|---|---|
| `full_url` | ✓ | string | http(s) URL to the full-res image (hotlinked from the publisher) |
| `license` | ✓* | string | SPDX-ish id (`CC0-1.0`, `CC-BY-4.0`, `PD`, `proprietary`, …). *Required unless the manifest sets `default_license`.* |
| `thumbnail_url` | | string | |
| `width` / `height` | | int | pixels; the app prefers ≥2000px long-edge for 4K displays |
| `format` | | string | MIME, e.g. `image/jpeg` |
| `focal_point` | | `[x, y]` | normalized 0–1 — the "most important point" the renderer keeps in frame when cropping to any panel aspect (16:9 TV, portrait e-ink, Frame). |
| `aspect_crops` | | object | OPTIONAL explicit per-shape crop presets — up to four normalized boxes `[x0, y0, x1, y1]` (0–1), keyed by screen shape: `"16:9"`, `"9:16"`, `"4:3"`, `"3:4"`. A focal point can only *slide* a fixed-size crop window; it can't *choose* one, and museum art clusters near-square, so an uncomposed portrait crop of a square painting can discard over half the work. When present, the renderer picks the preset nearest the target panel's ratio instead of computing a focal-point cover crop (`epaper.pick_crop_for_aspect`). Any subset of the four keys may be present; absent keys fall back to the focal-point crop. Example: `{"16:9": [0.0, 0.1, 1.0, 0.66], "9:16": [0.28, 0.0, 0.72, 1.0]}`. |
| `attribution` | ✓ when license is `CC-BY*`/`CC-BY-SA*` | string | |
| `rights_holder` | | string | |

### `interpretation` asset (OPTIONAL)

Independently authored/licensed from the image.

| field | req | type | notes |
|---|---|---|---|
| `author` | ✓ | string | who wrote it (≠ the artist) |
| `license` | ✓ | string | its OWN license |
| `attribution` | ✓ when license is `CC-BY*` | string | |
| `sections` | | array of `{heading, body}` | extended "Learn More" text |
| `points_of_interest` | | array of `{bbox, title?, commentary}` | `bbox` = `[x, y, w, h]` normalized 0–1; the kiosk pans/zooms here |
| `audio` | | array of `{url, license, language?, voice?, attribution?}` | narration tracks; each independently licensed |

## Rights summary (what we ask a creator for)

- For an **image**: a URL + a license. Attribution required for CC-BY(-SA).
- For **interpretation**: an author + a license, separate from the image. Attribution required for CC-BY(-SA).
- We **index pointers, never host** the bytes, and touch no money in v1 — so the creator keeps 100%
  and full control (broken/removed URL degrades gracefully).

## Status

- **v2.0 (now):** `image` + placard + per-asset license; `interpretation` is specified but
  optional and unpopulated. Federation core (subscribe-by-URL, signed manifests, registry) builds on this.
- The `interpretation` block may be populated by future tooling — additively, with no schema break.
