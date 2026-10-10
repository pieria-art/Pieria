# Publishing packs and catalog thumbnails (R2 / packs.curwe.ai)

Operator procedure for the official registry (bucket `screendocent-packs`, served at `packs.curwe.ai`).
Credentials: Infisical project `Screen-Docent` / env `prod` (`R2_ENDPOINT`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`); `SD_PACK_SIGNING_KEY` is Strongbox-only. Upload with `rclone :s3:` (wrap the call in
`bash -c '...'` with single quotes under `infisical run`). rclone-to-R2 may log a `501` then succeed on
attempt 2 - check the live result, not the log. Always pass `--s3-no-check-bucket`: the R2 key is object-scoped, so a single-file `copyto` otherwise dies on `CreateBucket 403 AccessDenied`. Cloudflare Bot Fight Mode on `curwe.ai` must stay OFF (ADR-136).

## Packs

1. Build + sign (`tools.build_pack`, `--signing-key`), then `python -m tools.verify_pack_offline --pack ./art-pack`.
2. `python -m tools.publish_pack --pack ./art-pack --out ./art-pack-dist` (regenerate pins only with
   `--pins-ref bd25595`, ADR-130).
3. Upload order: tars -> `covers/` -> `manifests/` -> `packs.json` LAST.
4. Purge the changed URLs in Cloudflare, then verify by streaming each tar through the PUBLIC domain and
   comparing sha256 to `packs.json` (headers and sizes prove nothing).

## Catalog thumbnails (ADR-148)

The bundled catalog's `thumbnail_url` points at `https://packs.curwe.ai/thumbs/<sha256(source_url)>.jpg`;
the old hotlink stays in `thumbnail_source_url` (provenance + the admin `onerror` fallback).

1. Build (offline, read-only on the pack, one image at a time; never copy `art-pack/`):
   `python -m tools.publish_thumbs --pack ./art-pack --out ./art-pack-dist/thumbs`
   Writes `thumbs/<hash>.jpg` (~600px, q82, progressive, sRGB, no EXIF) + `thumbs/index.json`
   (`source_url -> {path, sha256, bytes, origin}`). Prefers the pack's own `_catalog_thumbs`, then the local
   masters; `--fetch-remote` (last resort) downloads originals through the pack downloader.
2. Upload `thumbs/*` (including `index.json`) to `:s3:screendocent-packs/thumbs/` **before** the catalog/app
   release that points at them, e.g.
   `infisical run --projectId d274aa59-853d-48aa-bf82-dcbdcad0b2ba --env prod -- bash -c 'rclone --s3-provider=Cloudflare --s3-endpoint="$R2_ENDPOINT" --s3-access-key-id="$R2_ACCESS_KEY_ID" --s3-secret-access-key="$R2_SECRET_ACCESS_KEY" --s3-no-check-bucket --header-upload "Content-Type: image/jpeg" --header-upload "Cache-Control: public, max-age=31536000, immutable" copy ./art-pack-dist/thumbs :s3:screendocent-packs/thumbs/ --exclude index.json && rclone ... copyto ./art-pack-dist/thumbs/index.json :s3:screendocent-packs/thumbs/index.json'`
3. Purge `thumbs/` in Cloudflare (thumb names are content-addressed by source URL, so a re-encode
   under the same name needs the purge).
4. Spot-check through the public domain: pick ~10 entries from `index.json`, `curl -s https://packs.curwe.ai/<path> | sha256sum`
   and compare to the entry's `sha256`; confirm `content-type: image/jpeg`.
5. Only then merge/release the catalog-JSON commit (`python -m tools.publish_thumbs ... --rewrite-catalog`
   produces it; `tools/build_catalog.py --r2-thumbs ./art-pack-dist/thumbs` re-applies it after a catalog rebuild).

Installed collections never hit the network for thumbs: `GET /api/catalog/thumb/<hash>` serves the installed
pack's `_catalog_thumbs/<hash>.jpg`, and the catalog routes point matching items at it.
