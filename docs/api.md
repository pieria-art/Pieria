# Control your Pieria

Pieria has a small, documented, token-authenticated HTTP API. It is what the Home Assistant
integration is built on, and it is yours to use: read what every display is showing, skip to the next
painting, switch collection, search the library, change the night schedule.

- **Base URL:** `http://pieria.local:8000/api/v1` (or `http://<your-server>:8000/api/v1`)
- **Format:** JSON in, JSON out. Times are UTC ISO-8601.
- **Generated reference:** every endpoint, field and example, always in step with the server you are
  running, lives at **`/api/v1/docs`** (interactive) and **`/api/v1/openapi.json`** (machine-readable).
  This page is the guide; that one is the reference.

## The stability promise

Everything under `/api/v1` is **additive only**. Within v1 we add endpoints, optional request fields,
response fields and enum values, and we never rename, remove or retype anything. Write your client to
**ignore unknown response fields** and to **tolerate new values** in enums (a `command` action or a
display `kind` you have not seen before). A breaking change would be a new `/api/v2`, with v1 kept
running alongside it.

## 1. Create a token

Every call needs a token. Tokens are made in the admin, by someone who can open it:

1. Open **Admin → Settings → API & Integrations**.
2. Give the token a name (what will use it, e.g. "Home Assistant") and pick its **scopes**.
3. Press **Create token** and **copy it right away**. It is shown **once**; Pieria stores only a hash,
   so a lost token cannot be recovered. Revoke it and make a new one.

Revoking a token (same panel) takes effect immediately, and the panel shows when each token was last
used. Make one token per integration so you can cut one off without touching the rest.

### Scopes

| Scope | Allows |
|---|---|
| `read` | every `GET`: server info, displays, playlists, artwork, search, schedule |
| `control` | commands to displays and changes to the schedule (`POST`, `PATCH`) |

The scopes are independent: a `control` token cannot read unless it also has `read`. A token missing
the scope a call needs gets `403 insufficient_scope`.

## 2. Authenticate

Send the token as a bearer token on every request:

```
Authorization: Bearer pieria_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

```bash
export PIERIA=http://pieria.local:8000/api/v1
export TOKEN=pieria_xxxxxxxx   # the token you copied
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/info
```

Treat a token like a password: anyone on your network who has it can use it. Do not commit it or paste
it into screenshots.

## 3. Errors

Every error, from every endpoint, has one shape:

```json
{"error": {"code": "not_live", "message": "display is not live (not checking in); command not queued"}}
```

Branch on `code` (stable); `message` is for humans and may change.

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | `unauthorized` | Missing, invalid or revoked token. Carries `WWW-Authenticate: Bearer`. |
| 403 | `insufficient_scope` | The token is valid but lacks the scope this call needs. |
| 404 | `not_found` | No such display, artwork or playlist. |
| 409 | `not_live` | The display is not checking in, so the command was **not** queued. |
| 422 | `validation_error` | A field is missing, malformed or out of range (`message` says which). |
| 422 | `unknown_playlist` | `set_playlist` named a playlist that does not exist. |
| 500 | `internal_error` | Something broke on the server. Safe to retry later. |

New codes may be added; treat an unknown one as a generic failure of its HTTP class.

## 4. Discovery

On a Pieria appliance (the Raspberry Pi image) the box announces itself on your network over mDNS /
Bonjour as **`_pieria._tcp`** on port 8000, with these TXT records:

| TXT | Value |
|---|---|
| `version` | the server's release, e.g. `1.0.6` |
| `server_id` | the stable id of this server (same as `GET /info`), safe to use as a unique id |
| `path` | `/api/v1` |

Browse for it with, for example, `avahi-browse -rt _pieria._tcp` (Linux) or `dns-sd -B _pieria._tcp`
(macOS). Docker-compose installs do not run avahi: enter the host and port by hand.

## 5. Endpoints

All examples assume `PIERIA` and `TOKEN` from above. Add `-H "Content-Type: application/json"` to the
calls with a body.

### Server

**`GET /info`** (`read`): name, version, `api_version`, `appliance_mode`, `server_id`, `display_name`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/info
# {"name":"Pieria","version":"1.0.6","api_version":1,"appliance_mode":true,
#  "server_id":"123e4567-e89b-12d3-a456-426614174000","display_name":"living_room"}
```

### Displays

**`GET /displays`** (`read`): every display the server knows, with what each is showing. Includes
sleeping e-ink panels (`live: false`). It is a pure read and never advances playback.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/displays
```

```json
[{"id": "living_room", "live": true, "last_seen": "2026-10-04T20:15:02Z", "kind": "canvas",
  "playlist": "Impressionists", "mode": "ken-burns",
  "artwork": {"id": 42, "title": "The Starry Night", "artist": "Vincent van Gogh", "year": "1889",
              "image_url": "/artworks/42/display.jpg", "thumb_url": "/artworks/42/thumbnail",
              "is_personal": false}}]
```

`kind` is `canvas` (browser/kiosk), `eink`, `frame` (Samsung Frame TV) or `unknown`. `live` means the
display checked in within about 15 seconds; only live displays accept commands.

**`GET /displays/{id}`** (`read`): one display, same shape.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/displays/living_room
```

**`POST /displays/{id}/commands`** (`control`): send a command. Returns `202 {"status": "queued"}`:
it is queued for the display, not yet shown (a live display acts within about a second). If the display
is not live you get `409 not_live` and nothing is queued.

| `action` | Extra fields | Effect |
|---|---|---|
| `next` | | next artwork |
| `previous` | | previous artwork |
| `show_placard` | | show the museum placard for the current work |
| `set_playlist` | `playlist` (a name from `GET /playlists`) | switch collection |
| `set_mode` | `mode`: `ken-burns`, `static-crop` or `contain-matte` | change how art is rendered |

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"next"}' $PIERIA/displays/living_room/commands

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"set_playlist","playlist":"Impressionists"}' $PIERIA/displays/living_room/commands

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"set_mode","mode":"contain-matte"}' $PIERIA/displays/living_room/commands
```

### Library

**`GET /playlists`** (`read`): a light list (no artworks embedded): `id`, `name`, `artwork_count`,
`display_time` (seconds per artwork), `shuffle`, `default_mode`, `is_personal`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/playlists
```

**`GET /playlists/{id}/artworks?limit=50&offset=0`** (`read`): a page of a playlist's approved works
(`limit` 1-200). Returns `{total, limit, offset, items: [...]}`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$PIERIA/playlists/3/artworks?limit=20&offset=40"
```

**`GET /artworks/{id}`** (`read`): one artwork with its placard text, metadata (`creation_date`,
`culture`, `medium`, `tags`, `resolution_tier`) and a `license` object. When
`license.requires_attribution` is true (CC BY works) you must show `license.attribution` if you
re-display the image.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/artworks/42
```

**`GET /search?q=...&limit=20`** (`read`): text search over the approved library (title, artist,
movement/period, series, medium, tags, placard). Every whitespace-separated term must match; title and
artist hits rank first. `limit` 1-100.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$PIERIA/search?q=monet+water&limit=5"
```

### Schedule (night mode and quiet hours)

**`GET /schedule`** (`read`) returns the schedule; **`PATCH /schedule`** (`control`) changes any subset
of it (omitted fields keep their value). Times are `HH:MM`, 24 h, in the server's local time.
`day_brightness` and `night_brightness` are 0.1-1.0, `night_warmth` 0.0-1.0, `quiet_mode` is `cec` or
`blackout`. An out-of-range value is `422 validation_error`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/schedule

curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"quiet_enabled":true,"quiet_start":"23:00","quiet_end":"07:00"}' $PIERIA/schedule
```

**`GET /schedule/state`** (`read`): what the display should look like right now (`brightness`,
`warmth`, `quiet`, `quiet_mode`).

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/schedule/state
```

### Coming next (additive, still v1)

These are being added to v1 now. They are listed so you can plan for them; check `/api/v1/docs` on your
server for what it actually supports.

- **Pause and resume.** The command `action` enum gains `pause` and `resume`, and `Display` gains
  `paused` (boolean). While paused, a display holds its current artwork; an explicit `next` or
  `previous` still moves it once.
- **`POST /displays/{id}/show`** (`control`) with `{"artwork_id": 42}`: show a specific artwork now.
- **`GET /quiet`** (`read`) and **`POST /quiet`** (`control`) with `{"on": true, "until": "..."}`: a
  manual quiet override ("art off while we are out"). The response is
  `{"active": bool, "source": "schedule" | "manual" | "none", "until": ...}`.

## 6. Media (images)

Artwork objects carry **server-absolute paths**: `image_url` (display-sized JPEG) and `thumb_url`
(thumbnail). Resolve them against the server's base, **not** `/api/v1`:

```bash
curl -s -o painting.jpg http://pieria.local:8000/artworks/42/display.jpg
```

Image URLs are **not** token-protected (they are the same URLs the displays load), so you can hand them
straight to an `<img>` tag or a media player. Do not use `/next-image` or `/display/{id}/current.*` to
"look" at a display: those advance it. Use `GET /displays/{id}` instead.

## 7. Polling guidance

There is no push channel yet, so integrations poll.

- Poll **`GET /displays` no faster than every 5 seconds**; 10 s is plenty for a dashboard. One call
  returns every display, so do not poll each display separately.
- Library data (`/playlists`, `/artworks/{id}`, `/search`) changes rarely. Cache it; refresh on demand.
- After a command, wait about a second, then re-read `/displays`.
- If you see `401`, stop and ask the user for a new token rather than retrying.

Home Assistant users do not need any of this: the integration handles polling, discovery and
re-authentication for you.
