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
| 404 | `not_found` | No such display, artwork or playlist (or an artwork that is not approved). |
| 409 | `not_live` | A canvas display is not checking in, so the command was **not** queued. |
| 409 | `unsupported_for_kind` | A canvas-only action was sent to an e-ink or Frame display (see commands below). |
| 422 | `validation_error` | A field is missing, malformed or **out of bounds** (ids and offsets have ranges, `limit` is capped, `until` must be in the future); `message` says which. |
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

**`GET /info`** (`read`): name, version, `api_version`, `appliance_mode`, `server_id`, `display_name`,
plus two fields clients should use:

- **`api_features`**: the capabilities this server offers (today `pause`, `show`, `quiet`). **Feature-detect on
  this list, not on `version`**: an older Pieria simply will not list a feature, and new values are only ever added.
- **`token_scopes`**: the scopes of the token making the call (`read`, `control`), so a client can tell
  at once whether it may send commands.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/info
# {"name":"Pieria","version":"1.0.6","api_version":1,"appliance_mode":true,
#  "server_id":"123e4567-e89b-12d3-a456-426614174000","display_name":"living_room",
#  "api_features":["pause","show","quiet"],"token_scopes":["read","control"]}
```

### Displays

**`GET /displays`** (`read`): every display the server knows, with what each is showing. Includes
sleeping e-ink panels (`live: false`). It is a pure read and never advances playback.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/displays
```

```json
[{"id": "living_room", "name": "living_room", "live": true, "last_seen": "2026-10-04T20:15:02Z",
  "updated_at": "2026-10-04T20:14:51Z", "kind": "canvas", "paused": false,
  "playlist": "Impressionists", "playlist_id": 3, "mode": "ken-burns",
  "artwork": {"id": 42, "title": "The Starry Night", "artist": "Vincent van Gogh", "year": "1889",
              "image_url": "/artworks/42/display.jpg", "thumb_url": "/artworks/42/thumbnail",
              "is_personal": false}}]
```

- `kind` is `canvas` (browser/kiosk), `eink`, `frame` (Samsung Frame TV) or `unknown`.
- `live` means the display checked in within about 15 seconds. Canvas displays must be live to take most
  commands; e-ink and Frame displays sleep between pulls (`live: false`) and that is normal.
- `name` is a friendly label for your UI. Today it equals `id`; show it, but always address a display by `id`.
- `playlist` is the playlist name and `playlist_id` its id (null if unknown or since renamed); `set_playlist`
  accepts either.
- `paused` is the server-side pause flag (see `pause` below).
- `updated_at` (UTC) is when the display's now-playing, `paused` flag or `mode` last changed. After sending
  a command, compare it with the value you had to tell the command has taken effect.

**`GET /displays/{id}`** (`read`): one display, same shape.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/displays/living_room
```

**`POST /displays/{id}/commands`** (`control`): send a command. Returns `202 {"status": "queued"}`.
202 means accepted, not yet shown (a live canvas acts within about a second).

| `action` | Extra fields | Effect |
|---|---|---|
| `next` | | next artwork |
| `previous` | | previous artwork |
| `pause` | | hold the current artwork |
| `resume` | | carry on rotating |
| `show_placard` | | show the museum placard for the current work |
| `set_playlist` | `playlist` (name) **or** `playlist_id` (id), from `GET /playlists` | switch collection |
| `set_mode` | `mode`: `ken-burns`, `static-crop` or `contain-matte` | change how art is rendered |

How kinds differ:

- **`pause` / `resume`** work on every kind. They set a server-side flag first, so they succeed (202) even
  on a sleeping e-ink or Frame display, which honours the flag on its next pull. While paused, the display's
  own auto-advance re-serves the current artwork; an explicit `next` or `previous` still moves it once.
  A canvas also gets the command relayed so it stops or starts its own timer.
- **`next`, `previous`, `show_placard`, `set_playlist`, `set_mode` are canvas-only.** Sent to an e-ink or
  Frame display they return **`409 unsupported_for_kind`**, because those have no live channel.
- A canvas that is not checking in returns **`409 not_live`** and nothing is queued. A display whose kind is
  still `unknown` is treated as a canvas.
- **Queued commands expire after 60 seconds.** A command a display does not collect within a minute is
  dropped, never delivered late.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"next"}' $PIERIA/displays/living_room/commands

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"pause"}' $PIERIA/displays/living_room/commands

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"set_playlist","playlist_id":3}' $PIERIA/displays/living_room/commands

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"set_mode","mode":"contain-matte"}' $PIERIA/displays/living_room/commands
```

**`POST /displays/{id}/show`** (`control`) with `{"artwork_id": 42}`: show a specific approved artwork now.
Returns `202`. On a canvas it appears within about a second and the rotation continues from it. On
e-ink or Frame it becomes the next item served on the display's next pull and waits for it, even while
the display sleeps, for up to 6 hours. A paused display stays paused. `404` for an unknown display or an
unknown or unapproved artwork; `409 not_live` for a canvas that is not checking in.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"artwork_id":42}' $PIERIA/displays/living_room/show
```

### Library

**`GET /playlists`** (`read`): a light list (no artworks embedded): `id`, `name`, `artwork_count`,
`display_time` (seconds per artwork), `shuffle`, `default_mode`, `is_personal`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/playlists
```

**`GET /playlists/{id}/artworks?limit=50&offset=0`** (`read`): a page of a playlist's approved works
(`limit` 1-200, `offset` 0-1000000). Returns `{total, limit, offset, items: [...]}`.

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

### Quiet (manual override)

"Art off while we are out, on when we are home." A manual override sits on top of the quiet-hours
schedule.

**`GET /quiet`** (`read`): is the display quiet right now, and why. Returns

```json
{"active": true, "mode": "on", "source": "manual", "until": "2026-10-04T23:00:00Z"}
```

- `active`: quiet (panel off or blank) right now, whatever the cause.
- `mode`: the override in force: `on`, `off`, or `auto` when there is none and the schedule rules.
- `source`: `manual` (an override is in force, and it may force quiet **off** too), `schedule` (the
  quiet-hours schedule is making it quiet) or `none`.
- `until` (UTC): when this state ends: the override's expiry (null = indefinite) or the end of the current
  scheduled quiet window; null when `source` is `none`.

**`POST /quiet`** (`control`) with `{"mode": "on" | "off" | "auto", "until": "<ISO-8601>"}` (`until`
optional) returns the resulting state, same shape as `GET /quiet`.

| `mode` | Effect |
|---|---|
| `on` | force quiet now (canvas blackout; the appliance also powers the panel off when the schedule's `quiet_mode` is `cec`) |
| `off` | force awake now, even inside quiet hours |
| `auto` | clear the override; the schedule rules again (`until` is ignored) |

For `on` and `off` the override **ends at `until` if you give one, otherwise at the next scheduled quiet
boundary, whichever comes first**, so a forced state never sticks silently. With no quiet schedule
configured it lasts until you change it. `until` must be in the future (an offset-less value is read as
UTC), or you get `422 validation_error`. It takes effect on the display's next schedule poll (about 60 s
for a canvas).

```bash
curl -s -H "Authorization: Bearer $TOKEN" $PIERIA/quiet

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"on","until":"2026-10-04T23:00:00Z"}' $PIERIA/quiet

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"auto"}' $PIERIA/quiet
```

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
- Poll `/quiet` no faster than every 10 seconds (it only takes effect on a ~60 s display poll anyway).
- Library data (`/playlists`, `/artworks/{id}`, `/search`) changes rarely. Cache it; refresh on demand.
- After a command, wait about a second, then re-read `/displays` and watch `updated_at`. Commands expire after 60 s, so do not retry a `202` blindly.
- If you see `401`, stop and ask the user for a new token rather than retrying.

Home Assistant users do not need any of this: the integration handles polling, discovery and
re-authentication for you.
