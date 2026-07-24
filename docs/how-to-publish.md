# How to publish a collection

Pieria is **federated**: anyone can publish a collection of their own artwork as a
**Manifest v2** JSON file, host it anywhere public, and any other Pieria can subscribe to it.
You keep your work on your own hosting — Pieria indexes *pointers*, it never hosts your images
or takes a cut. (See [`manifest-v2.md`](manifest-v2.md) for the schema; the validator
`tools/manifest_validator.py` is the source of truth.)

There are two ways to author a feed — a GUI (**Publisher Studio**) and a CLI (**`build_manifest`**).
Both share one engine (`publisher.py`), so they produce identical manifests. Pick whichever fits.

---

## 1. Get a publisher identity (once)

Your identity is an **Ed25519 keypair**. The private key signs your manifests; the public key proves
they're yours and untampered. The private key is your long-lived identity — **keep it secret**, and
note that *rotating it invalidates the signature on everything you've already published*.

- **Studio:** open `/publisher`, fill in your publisher **ID** (a stable slug like `jane-doe`),
  **display name**, and optional homepage, then **Save identity**. A keypair is generated and stored
  on *your own* server (never sent to a browser). The panel shows your **public key** — you'll need it
  for verification (step 5).
- **CLI:** `python -m tools.sign_manifest keygen` — prints a private + public key. Keep the private
  key somewhere safe (a password manager / secret store).

## 2. Author your collection

Each item is an artwork + the **public URL of the image you host yourself** + its metadata and license.

- **Studio:** create a collection (title, optional slug, description, default license). For each
  artwork: paste its public image URL (it previews and reads the dimensions client-side), tap the
  image to set the **focus point** (how it's framed on any screen), and fill in title, artist, date,
  medium, tags, placard, and per-item license/attribution. The validation panel tells you when it's
  ready.
- **CLI:** put one artwork per row in a CSV and run `build_manifest`:

  ```bash
  python -m tools.build_manifest --csv items.csv --meta meta.json --out my-collection.json
  ```

  **`items.csv`** columns (only `image_url` + `title` are required; unknown columns are ignored):

  ```
  image_url, title, artist, artist_role, date, creation_date, medium, culture,
  tags, placard, license, attribution, rights_holder, thumbnail_url,
  width, height, focal_x, focal_y, id
  ```
  - `tags` — `|`- or `,`-separated (use `|` if a tag contains a comma).
  - `focal_x`/`focal_y` — floats 0–1; both needed to set a focus point.
  - `width`/`height` — pixels (integers).

  **`meta.json`** describes the collection:

  ```json
  {
    "slug": "janes-impressionists",
    "title": "Jane's Impressionists",
    "description": "A small personal selection.",
    "default_license": "CC0-1.0",
    "publisher": { "id": "jane-doe", "name": "Jane Doe", "url": "https://jane.art" }
  }
  ```

**Licensing rules** (enforced by the validator): every image needs a license — either per item or via
the collection's `default_license`. Use SPDX-ish ids: `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`, `PD`,
`proprietary`. **`CC-BY*` and `CC-BY-SA*` require an `attribution`.** You declare the license; you're
attesting you have the right to — Pieria can't verify copyright (no platform can), it verifies
*identity* and honors takedowns.

## 3. Sign it

- **Studio:** click **Export & sign** — the manifest is validated and signed server-side with your key,
  and downloads as `<slug>.json`.
- **CLI:** add `--key <your-private-key-base64>` to the `build_manifest` command. (Without `--key` the
  manifest is published *unsigned* — that's fine; it just stays in the **community** tier, below.)

## 4. Host it

Upload your `manifest.json` **and your images** to any public hosting — GitHub raw, an object store
(S3/R2/B2), or your own site. The manifest URL must:

- return **`application/json`** (not an HTML page),
- **not redirect** (subscribe to the final URL directly — redirects are refused for safety),
- be **≤ 5 MB** with **≤ 5000 items**,
- resolve to a **public** address (loopback/private IPs are rejected).

Your images stay on your host and are hotlinked; if a URL breaks, that item just degrades gracefully.

## 5. Let people subscribe — and (optionally) get verified

Anyone pastes your manifest URL into **Admin → Subscriptions**. It's fetched, validated, and (if
signed) signature-checked, then merged into their browse view tagged **Community** (trust-on-first-use).
That's the whole flow — no gatekeeping.

To earn the **Verified** badge, your signed feed's key has to be in the curated registry
(`registry/trusted_publishers.json`, a map of `publisher.id → public_key`). Today that's done
out-of-band: send your `publisher.id` + public key (or open a PR adding the entry), and once it's
merged and shipped, subscribers on that build see your feed as **Verified**.

> **⚠ Onboarding note (to be improved):** verification currently means hand-editing a static JSON file
> in the repo and redeploying — heavier than we want for onboarding artists. A lighter-weight
> verification/registry path is an open item. For now, community-tier (unsigned or self-signed) works
> end-to-end and needs nothing from us.

---

## Trust tiers at a glance

| Tier | What it means |
|---|---|
| **Verified** | Signed, and the key matches the curated registry for that publisher id. |
| **Community** | Unsigned, or validly self-signed but not (yet) in the registry. Fully functional. |
| *(rejected)* | A present-but-invalid signature = tampered → refused outright. |
