# MMM-Pieria

A [MagicMirror²](https://magicmirror.builders/) module that turns a slot on your mirror into a
rotating museum wall: it shows the **current curated artwork and its placard** (title, artist, date,
medium, and an AI-written blurb) from a running [Pieria](https://github.com/AiwendilInTheWoods/Pieria)
server.

It's a thin client over Pieria's existing display feed — **no extra server setup**. The module
asks the server what to show next on a timer, honoring the server's own per-artwork display time, and
the server's curation brain (shuffle + affinity weighting) decides the rotation.

## Preview it first (no MagicMirror required)

Open [`preview.html`](preview.html) in any browser, point it at your Pieria server (default
`http://localhost:8000`), and you'll see exactly what the module renders. Handy for confirming your
server URL and picking a playlist before you wire it into MagicMirror.

## Install

From your MagicMirror folder:

```bash
cd ~/MagicMirror/modules
# Option A — copy just this folder (e.g. from a Pieria checkout):
cp -r /path/to/Pieria/integrations/MMM-Pieria .
# Option B — clone Pieria and symlink the module:
# git clone https://github.com/AiwendilInTheWoods/Pieria.git
# ln -s Pieria/integrations/MMM-Pieria MMM-Pieria
```

No `npm install` needed — the module is front-end only.

## Configure

Add a block to the `modules` array in `~/MagicMirror/config/config.js`:

```js
{
  module: "MMM-Pieria",
  position: "fullscreen_below", // full-bleed art backdrop + corner placard
  config: {
    serverUrl: "http://192.168.1.50:8000", // your Pieria server
    playlist: "",                          // blank = the server's first playlist
    displayId: "magicmirror"
  }
}
```

Prefer a contained card? Use a normal region like `position: "top_left"` instead of
`fullscreen_below`.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `serverUrl` | `"http://localhost:8000"` | Base URL of your Pieria server. |
| `playlist` | `""` | Playlist/collection name to show. Blank = the server's first playlist. |
| `displayId` | `"magicmirror"` | Identifies this display to the server's rotation state. Give each mirror a unique id if you run more than one. |
| `updateInterval` | `0` | Milliseconds between artworks. `0` honors the server's per-artwork `display_time`. |
| `minRefresh` | `15000` | Hard floor (ms) on refresh rate, to be kind to the server. |
| `retryDelay` | `20000` | How long to wait (ms) before retrying after a connection error. |
| `showImage` | `true` | Show the artwork image. |
| `showPlacard` | `true` | Show the placard (title/artist/etc.). |
| `showDescription` | `true` | Include the AI-written blurb on the placard. |
| `maxDescriptionChars` | `0` | Truncate the blurb to this many characters (`0` = no limit). |
| `crossfade` | `true` | Crossfade when the artwork changes. |
| `imageMaxHeight` | `null` | Cap image height, e.g. `"70vh"` or `"600px"`. |

## How it works (and a note on rotation)

The module calls the server's `GET /next-image` endpoint, which returns the image URL, the placard
metadata, and a `display_time`. **Each call advances the rotation** for this `displayId`, so the
module fetches exactly once per cycle — it never rapid-polls. If you point two mirrors at the same
server, give them different `displayId` values so they keep independent positions.

If the server is briefly unreachable, the module keeps showing the last artwork and retries.

## Possible enhancements

- A `node_helper.js` for server-side fetching (not needed today — Pieria serves open CORS).
- Live push over the Pieria WebSocket (`/ws/{display_id}`) instead of timer-based fetch.
- Targeting a specific `displayId` from the Pieria admin remote.

## License

AGPL-3.0 — part of the [Pieria](https://github.com/AiwendilInTheWoods/Pieria) project.
