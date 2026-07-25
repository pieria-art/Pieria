# 🖼️ Pieria

[![CI](https://github.com/pieria-art/Pieria/actions/workflows/pytest.yml/badge.svg)](https://github.com/pieria-art/Pieria/actions/workflows/pytest.yml)
[![Sources](https://github.com/pieria-art/Pieria/actions/workflows/verify-sources.yml/badge.svg)](https://github.com/pieria-art/Pieria/actions/workflows/verify-sources.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](LICENSE)

**Pieria** is an open-source, AI-powered digital art curator that turns any TV, monitor, or e-ink
panel into a high-end museum display — with autonomous artwork analysis, museum-grade placards, and
instant mobile remote control.

![A Pieria display showing Leonardo da Vinci's The Last Supper with an auto-generated museum placard and a QR code linking to more detail](static/docs/display.png)

> *A live display: full-bleed artwork, an auto-generated museum placard, and a QR code for details.*

**Own your art wall.** No subscription, no ads, no cloud account, no vendor that can switch it off. The
polished art frames — Meural, Depict, Canvia — are closed, subscription-locked, and several have
**bricked their customers' hardware** when the company moved on. Pieria is the opposite: it runs
on hardware you already own (or a $35 Pi), keeps working with no internet, and is yours to keep. Flash
one image, set it up from your phone, and a curated public-domain museum gallery is on the wall in
minutes — **or** run the curation brain on a server you control and point any screen at it.

## ✨ Features

*   **🕵️ Museum Art Scouts:** Effortlessly search and pull high-res masterpieces directly from world-class APIs (The Met, Art Institute of Chicago, SMK, Cleveland, Rijksmuseum) straight into your discovery queue — plus **NASA** space photography and **Wikimedia Commons** (the broadest public-domain pool, filtered to PD works at display-grade ≥2000px). All keyless. Supports premium integrations for Harvard Art Museums, Smithsonian, and Europeana.
*   **🧠 Vision RAG Curator:** Automatically generates museum-grade VRA Core metadata for all artworks. The system features a built-in multilingual translation pipeline that automatically converts foreign metadata (e.g., Dutch Open Data from the Rijksmuseum) into fluent English using Gemini's visual grounding.
*   **🏛 VRA Core Database:** Built on the established Visual Resources Association schema, securely housing rich metadata alongside dynamic crop data and playlists. Supports Many-to-Many relationships for flexible artwork-to-playlist mapping and custom sequencing.
*   **📱 WebSocket Remote:** A mobile-first, no-refresh PWA remote to switch playlists, change modes, and trigger placards instantly.
*   **📺 Multi-Display Support:** Targeted routing using unique display IDs allows a single server to manage different artwork streams across multiple TVs.
*   **📦 Flash-and-go appliance (can't be bricked):** A pre-baked Raspberry Pi image sets itself up from your phone over a captive-portal Wi-Fi hotspot — no SSH, no config files. If setup is interrupted or Wi-Fi is wrong, it re-opens its own hotspot instead of black-screening. **Self-updating** without a terminal: the admin page notifies you when a new release ships (with notes) and updates on one click — you decide when, nothing auto-installs. No cloud account, no subscription, nothing a vendor can switch off.
*   **🎨 Advanced Rendering:** Choose between cinematic Ken Burns pans, static user-defined crops, or blurred matte effects. The Ken Burns pan is **focal-point-aware** — every artwork carries a focal point (AI-derived, or tap-to-set) so off-center subjects, like a portrait's face, stay framed instead of being slowly panned out of view.
*   **📸 My Photos (Studio):** A phone-first studio to put your *own* photos on the wall — multi-upload (with camera capture), optional AI captions (in a warm photo-album voice, with an honest on-device-vs-cloud privacy note), and tap-to-set framing. **iPhone HEIC photos work as-is** (auto-converted on upload). Your photos are stored **locally on your server** — never uploaded to anyone's cloud, never indexed — and shown with a clean caption (zero museum jargon).
*   **⚖️ Hierarchical Config:** Precise control via URL parameters that override playlist and global defaults.
*   **🔒 Human-in-the-Loop:** Audit and refine AI-generated content before it goes live. Finalize a live find with **inline review** — its card expands in place into an editable placard that the AI fills in as you watch, Approve right there with no tab-hop — or batch it in the dedicated **Review Queue** (with **☑ Select → Approve & Publish** for many at once).
*   **🔌 Bring Your Own Model:** Configure the AI engine from the GUI — Google Gemini, OpenAI, Anthropic, OpenRouter (one-click sign-in), or a local Ollama/LM Studio server — all through one OpenAI-compatible backend. No code edits, validated live before saving.
*   **🖼️ Curated Art — own whole collections:** A collection-first library you *own*, not just browse. Under **🏛️ Curated Art**, add an official **pack** — Impressionism, the Dutch Golden Age, Ukiyo-e, Baroque, Ancient Egypt, Maps & Cartography, the Cosmos, and more — with one **Download**: the whole collection fetches once, verifies its Ed25519 signature, and plays fully **offline**; **Remove** reclaims the disk (works shared with another collection, and your own photos, are kept). **2,800+ public-domain works across 28 collections**, sourced from world-class museums, NASA, Wikimedia Commons, and the Smithsonian (CC0), every image display-grade, with ready-made placards and **pre-baked focal points** (so subjects stay framed). A **Curated Art** search box (title/artist **autocomplete**) finds a specific piece across your collections — already-owned works show an **Added ✓** tag — and, when it comes up short, escalates the same query to a **live museum search**. *Advanced:* regenerate/expand the underlying catalog with the offline builder (`python -m tools.build_catalog`) and **host it on any static URL** (GitHub raw, an object store, any web server), pointed at via **Settings → 📚 Catalog Source**.
*   **🌐 Federated Collections (beta):** Subscribe to a publisher's collection by URL and browse it alongside the bundled catalog, tagged **Official**, **Verified**, or **Community**. Feeds are an open [Manifest v2](docs/manifest-v2.md) format — we index pointers (images stay on the publisher's server), safety-check and validate every feed, and verify Ed25519 signatures for the *Verified* tier. Add one from the **➕ Subscribe** tile under **🏛️ Curated Art** (manage feeds under **Settings → 🌐 Subscriptions**). **Publishing your own?** Author and sign a collection in the **Publisher Studio** (`/publisher`) or from the command line (`python -m tools.build_manifest`) — see [How to publish](docs/how-to-publish.md).
*   **💾 Persistent & Safe:** SQLite-backed state with automatic migrations and Docker volume persistence.

## 🧭 Deployment Models

Pieria is a **curation brain** you run once (a small Docker app) + **any screen** you point at
it. The same server supports any mix of these at once — it's a versatile setup, not a single appliance:

| Model | What it is | Best for |
|-------|-----------|----------|
| **Run the brain anywhere** | Docker on a server / NAS / mini-PC / old laptop; point any screen at it. | The foundation for everything below. |
| **All-in-one Pi** | Server **and** display on one Raspberry Pi. | The simplest single, self-contained art frame. |
| **Thin-client Pi → server** | Cheap Pis display; one central server curates. | Scaling to many rooms / whole-home. |
| **Any browser / Smart TV** | Point a TV browser (or Fully Kiosk) at the display URL. | Reusing a TV you already own — no extra hardware. |
| **e-ink / "dumb" frame** | Low-power frames fetch a server-rendered, dithered image on a schedule (image API — see the e-ink section below). | Battery e-ink, DIY ESP32/Waveshare, TRMNL (BYOS). |
| **Multi-display** | One server drives many screens via unique `display` IDs. | A docent in every room, each remote-controllable. |

> **Which board, how much RAM, what it costs?** See the **[Hardware Profile](docs/hardware-profile.md)** — validated sizing by output type (TV, e-ink, satellite), soak-tested thermals, and current board prices.

![A quick tour of the admin dashboard: Curated Art, the collections grid, the full library, and galleries](static/docs/admin-tour.gif)

The full, illustrated guide (with screenshots and walkthroughs for each) lives in the in-app
**Help & Docs** page at `http://localhost:8000/help`.

## 🚀 Get Started — two front doors

Pick the one that matches what you want:

- **A. "I want an art frame."** Flash one image to a Raspberry Pi, set it up from your phone. No SSH, no
  config files, no Linux. → **[Flash the appliance image](#a-flash-the-appliance-image-art-in-minutes)**
- **B. "I want to run the curation server."** A small Docker app on a machine you already have; point
  any number of screens at it. → **[Run the brain with Docker](#b-run-the-brain-with-docker)**

Both run the same curation brain and can be mixed freely (one server, many screens).

### A. Flash the appliance image (art in minutes)

The **all-in-one Pieria Appliance** is a Raspberry Pi that *is* the whole thing — server and display in
one box. You never touch a terminal:

1. **Download** the latest image from the **[Releases page](https://github.com/pieria-art/Pieria/releases/latest)** (`pieria-<version>.img.xz`).
2. **Flash** it to a microSD card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (or Balena Etcher) — no OS-customisation needed.
3. **Boot** the Pi. The screen shows a setup card; join the **`Pieria-Setup`** Wi-Fi from your phone and a page opens automatically.
4. **Set up** — pick your Wi-Fi, name the display, choose orientation. It reboots, downloads its first gallery, and paints. Museum art on the wall, unattended.

> **It can't be bricked.** If Wi-Fi is mistyped or setup is interrupted, the box re-opens its own setup
> hotspot instead of stranding you on a black screen. There's no cloud account to lose and no vendor to
> shut it down — the whole system is on the card in your hand.
>
> **Updates, no SSH.** When a new release is out, the admin page shows it with release notes and a one-
> click **Update** — the box pulls it and rebuilds itself. You choose when; nothing auto-installs.

**Build the image yourself** (or roll your own for other boards) with a fresh Raspberry Pi OS + our
provisioner — see **[docs/image-build.md](docs/image-build.md)** and [`deploy/appliance/`](deploy/appliance/README.md).
A **display-only thin client** (Pi shows art, a server elsewhere curates) uses the same provisioner —
set `ALL_IN_ONE=0`.

### B. Run the brain with Docker

Run the curation server on anything with Docker — a NAS, a mini-PC, an old laptop — and point any screen at it.

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/install/). Then clone and go — no config files:

```bash
git clone https://github.com/pieria-art/Pieria.git
cd Pieria
docker compose up -d --build
```
(No `.env` required; the stack runs out of the box and you configure the AI model in-app below.)

**Access it:**
*   **Admin Dashboard:** `http://localhost:8000/admin` (upload, discover, and manage art)
*   **Main Display:** `http://localhost:8000/` (point a TV browser here, or a [display appliance](#a-flash-the-appliance-image-art-in-minutes))
*   **Mobile Remote:** `http://localhost:8000/remote` (control from your phone)

### Connect a Model (optional, in-app — applies to both paths)
Pieria works immediately without any AI — the starter art ships with full placards, and the
museum **Discover** scouts need no key. To unlock auto-curation (metadata generation, enrichment,
smarter search), open **Admin → ⚙ Settings → 🧠 AI Engine**, pick a provider (Google Gemini, OpenAI,
Anthropic, OpenRouter, or a local Ollama/LM Studio server), paste a key (or **Sign in with
OpenRouter** for one-click setup), and click **Test & Save** — validated live, effective in seconds.

> Prefer files? You can still pre-seed a default Gemini key with a `.env` (`GEMINI_API_KEY=…`) in the
> project root before launch; the in-app setting overrides it when set. (A *distributed* appliance image
> never ships a key — each owner adds their own.)

## 🖼️ e-ink & BYOS frames (image API)

Low-power e-ink and "bring-your-own-server" frames (DIY ESP32 + Waveshare, Inky Impression, a TRMNL
in BYOS mode) don't run the JS display — they just fetch a server-rendered image on a schedule:

```
GET /display/{display_id}/current.png?playlist=Masterpieces&w=1600&h=1200&palette=spectra6
```

The server runs the same curation brain (bag-shuffle + affinity), crops to the panel size, and
Floyd–Steinberg-dithers to the device palette. The response's **`X-Refresh-After`** header tells the
frame how long (seconds) to deep-sleep before the next fetch; each GET advances to the next image.

- **Palettes:** `spectra6` (E Ink Spectra 6), `acep7` (7-colour ACeP/Gallery), `gray4`, `gray16`.
- **Formats:** `.png` (default) or `.bmp` (firmware without a PNG decoder).
- **Fit:** `fit=cover` (fill, default) or `fit=contain` (letterbox on white).
- **Pimoroni Inky Impression Spectra 6:** a ready-made poll-and-sleep host client (`sd-eink`) drives the
  panel over SPI — change-detects on the response's `ETag`, floors refresh cadence, and never repaints
  during Night/Quiet Hours. See [`deploy/appliance/`](deploy/appliance/README.md#e-ink-panel-track-b-optional).

## 🔌 Integrations

Thin clients that render the same curation brain on platforms people already run:

- **[Samsung Frame TV](integrations/frame-tv/)** — push curated art into a Frame's **Art Mode** over
  your LAN (no Samsung account, no Art Store subscription). Configure it in **Settings → 🖼️ Samsung Frame TV**
  (IP, playlist, interval) and the server keeps the Frame updated, reusing the same curation brain.
  Turns a Frame you already own into another Pieria display.
- **[MagicMirror²](integrations/MMM-Pieria/)** — the `MMM-Pieria` module turns a slot on a
  smart mirror into a rotating museum wall (current artwork + placard from your server). Front-end
  only, no extra setup; includes a `preview.html` to try it in any browser without MagicMirror.

## 🏛️ VRA Core Metadata Architecture

Pieria utilizes the **Visual Resources Association (VRA) Core** schema for its internal SQLite database design (`models.py`). This guarantees museum-quality structural integrity.
Supported schema properties mapped automatically by the AI include:
*   `title`
*   `agent_name` & `agent_role` (e.g., Maker, Artist, Photographer)
*   `creation_date` & `date_display` (e.g., 'c. 1890', '19th century')
*   `cultural_context` (e.g., 'Dutch', 'Edo Period')
*   `medium` (e.g., 'Oil on canvas')
*   `description_narrative` (Generated 2-sentence museum blurbs)
*   `tags` (Automated visual extraction tags)

## 🛠️ Configuration Hierarchy

Pieria uses a strict priority system for settings like `cycle_time`, `mode`, and `shuffle`:

1.  **URL Parameters:** `?mode=static-crop&cycle_time=60` (Highest Priority)
2.  **Playlist Defaults:** Configured per collection in the Admin UI.
3.  **Global Defaults:** System-wide fallbacks.

## 📖 Documentation
For URL parameters, the **Raspberry Pi appliance** how-to, **e-ink / "dumb" frame** setup, and hardware tips, visit the internal **Help & Docs** page at `http://localhost:8000/help` from your running server.

## 🔐 Advanced: Enabling HTTPS (optional)

**Do you need it?** For a home LAN, usually **no**. The display/kiosk path carries no secrets, and the
app works fine over plain HTTP. Plain HTTP is genuinely sufficient for most setups — the "Not secure"
label is mostly perception. HTTPS earns its keep in two specific cases:

1. **You expose the admin beyond your home/trusted LAN**, or want to stop API keys you paste into
   **Settings → AI Engine** from crossing the wire in plaintext.
2. **You want OpenRouter's one-click sign-in.** It uses a browser crypto API that only works in a
   *secure context* (HTTPS **or** `http://localhost`). Over `http://<LAN-IP>` the button is hidden and
   you paste an OpenRouter key instead — HTTPS (or accessing via `localhost`) restores one-click.

**Why not bake certs into the app:** Pieria streams to headless browsers (Smart TVs, Pi
kiosks) that can't click through a cert warning — a self-signed cert on the Python backend breaks
them with un-bypassable `ERR_CERT_AUTHORITY_INVALID`. So TLS is terminated by an **opt-in reverse
proxy**, and **kiosks keep using plain `http://`** (localhost on an all-in-one box, or
`http://<server>:8000`). HTTPS is for the **human-facing admin/remote**.

### Turn it on (built-in Caddy profile)
```bash
docker compose --profile tls up -d           # adds an HTTPS proxy on :443; app stays on :8000
```
This runs [Caddy](https://caddyserver.com/) in front of the app ([`deploy/tls/Caddyfile`](deploy/tls/Caddyfile)).

**End-to-end, the two honest paths** (browser-trusted HTTPS on a LAN always requires *one* of these):

* **A. Real domain (lock icon everywhere, zero manual trust) — recommended if you can.**
  Point a domain you own at this host's IP (a free DNS name works; split-horizon/local DNS is fine
  for LAN-only), then:
  ```bash
  echo "SD_TLS_HOST=art.example.com" >> .env     # your domain
  # remove the `tls internal` line in deploy/tls/Caddyfile
  docker compose --profile tls up -d
  ```
  Caddy fetches a trusted Let's Encrypt certificate automatically (ports 80+443 must be reachable).

* **B. LAN-only with Caddy's internal CA (no domain) — you must trust the CA once per device.**
  Leave the default (`SD_TLS_HOST=pieria.local`, `tls internal`). Browsers will warn until you
  install Caddy's root CA on each phone/laptop that opens the admin:
  ```bash
  # grab the root CA Caddy generated, then trust it on your device(s)
  docker compose --profile tls cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
  ```
  Install `caddy-root.crt` into your OS/browser trust store (macOS Keychain, Windows "Trusted Root",
  iOS/Android profile). After that, `https://pieria.local` shows a clean lock. Per-device, but
  one-time. (Tools like [mkcert](https://github.com/FiloSottile/mkcert) automate the same idea.)

> **Reminder:** don't point a TV/kiosk at the internal-CA HTTPS URL — it can't accept the cert. Keep
> displays on `http://` and reserve HTTPS for the admin/remote you open yourself.

---
*Built for art lovers, powered by AI.*
