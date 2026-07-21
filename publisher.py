"""Publisher core — assemble, validate, and sign a Manifest v2 collection.

The shared engine behind BOTH the Publisher Studio (the `/api/publisher/*` routes in app.py) and the
`tools/build_manifest` CLI, so authoring-by-GUI and authoring-by-CSV emit byte-identical manifests
through one implementation. Deliberately dependency-light — stdlib + PyNaCl + the validator + the
canonical-bytes definition — so the CLI runs without FastAPI/SQLAlchemy installed.

Flow: author rows -> build_item() -> build_manifest() -> validate_manifest() -> sign_manifest().
The signature covers `federation.canonical_bytes` (the manifest minus `signature`, JSON sorted-keys +
compact + UTF-8) — signer and verifier MUST agree on exactly those bytes.
"""

from __future__ import annotations

import base64

from nacl.signing import SigningKey

from federation import canonical_bytes
from manifest_validator import MANIFEST_VERSION, validate_manifest


# ---------------------------------------------------------------- keys + signing
def keygen() -> tuple[str, str]:
    """Mint an Ed25519 keypair. Returns (private_b64, public_b64). The private key is the publisher's
    long-lived IDENTITY — keep it secret; rotating it invalidates every signature already published."""
    sk = SigningKey.generate()
    return base64.b64encode(bytes(sk)).decode(), base64.b64encode(bytes(sk.verify_key)).decode()


def public_from_private(private_b64: str) -> str:
    """Derive the base64 public key from a base64 private signing key."""
    return base64.b64encode(bytes(SigningKey(base64.b64decode(private_b64)).verify_key)).decode()


def sign_manifest(manifest: dict, private_b64: str, public_b64: str | None = None) -> dict:
    """Return a copy of `manifest` with `publisher.public_key` set and a `signature` over the canonical
    bytes. Does not mutate the input. `public_b64` is derived from the private key when omitted."""
    sk = SigningKey(base64.b64decode(private_b64))
    public_b64 = public_b64 or base64.b64encode(bytes(sk.verify_key)).decode()
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    pub = dict(signed.get("publisher") or {})
    pub["public_key"] = public_b64
    signed["publisher"] = pub
    signed["signature"] = base64.b64encode(sk.sign(canonical_bytes(signed)).signature).decode()
    return signed


# ---------------------------------------------------------------- item / manifest assembly
def _clean_str(v) -> str | None:
    s = (v or "").strip() if isinstance(v, str) else v
    return s or None


def _as_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _tags_list(v) -> list[str] | None:
    """Accept a list already, or a `|`- or `,`-separated string. Returns a clean list (or None)."""
    if v is None or v == "":
        return None
    if isinstance(v, list):
        items = v
    else:
        items = [t for chunk in str(v).split("|") for t in chunk.split(",")]
    out = [t.strip() for t in items if t and t.strip()]
    return out or None


def build_item(row: dict) -> dict:
    """Normalize one author row into a Manifest v2 item. Empty fields are omitted so the canonical
    bytes stay clean. Interpretation/access are intentionally NOT authored.

    Idempotent: accepts either a FLAT row (CSV / Studio payload, image fields at top level) or an
    ALREADY-NESTED item (a previously-built/stored item with an `image` sub-object), so it can be run
    again at validate/export time without losing data. Top-level (flat) values take precedence.
    """
    img_in = row.get("image") if isinstance(row.get("image"), dict) else {}

    def pick(*keys):
        for k in keys:
            v = _clean_str(row.get(k))
            if v is not None:
                return v
        return _clean_str(img_in.get(keys[-1]))

    # Asset address: a remote `full_url` (third-party feeds) and/or a `local_file` (first-party pack
    # that ships the bytes). At least one; both omitted-when-empty so the canonical bytes stay clean.
    image: dict = {}
    full = _clean_str(row.get("full_url") or row.get("image_url") or img_in.get("full_url"))
    if full is not None:
        image["full_url"] = full
    local = _clean_str(row.get("local_file") or img_in.get("local_file"))
    if local is not None:
        image["local_file"] = local
    for dst in ("thumbnail_url", "license", "attribution", "rights_holder"):
        val = pick(dst)
        if val is not None:
            image[dst] = val
    for src in ("width", "height"):
        val = _as_int(row.get(src) if row.get(src) not in (None, "") else img_in.get(src))
        if val is not None:
            image[src] = val
    fx, fy = _as_float(row.get("focal_x")), _as_float(row.get("focal_y"))
    fp = row.get("focal_point") or img_in.get("focal_point")
    if fx is not None and fy is not None:
        image["focal_point"] = [fx, fy]
    elif isinstance(fp, (list, tuple)) and len(fp) == 2:
        image["focal_point"] = [float(fp[0]), float(fp[1])]
    # Per-shape crop presets (epaper.ASPECT_CROP_KEYS): image-attached metadata, never baked, so it
    # lives beside focal_point. Flat row takes precedence over an already-nested item (idempotent).
    ac = row.get("aspect_crops")
    if not isinstance(ac, dict):
        ac = img_in.get("aspect_crops")
    if isinstance(ac, dict) and ac:
        image["aspect_crops"] = ac

    item: dict = {"id": _slugify(row.get("id") or row.get("title") or ""),
                  "title": _clean_str(row.get("title")), "image": image}
    for k in ("artist", "artist_role", "date", "creation_date", "medium", "culture", "placard",
              "series", "resolution_tier"):
        val = _clean_str(row.get(k))
        if val is not None:
            item[k] = val
    tags = _tags_list(row.get("tags"))
    if tags is not None:
        item["tags"] = tags
    return item


def build_manifest(meta: dict, items: list[dict], *, generated_at: str | None = None) -> dict:
    """Assemble a Manifest v2 collection dict from collection meta + author rows. Does NOT sign.

    `meta` keys: slug/id, title, description?, default_license?, publisher{id,name,url?}.
    `generated_at` is passed in (callers stamp it) because this module stays time-source agnostic.
    """
    publisher = meta.get("publisher") or {}
    manifest: dict = {
        "manifest_version": MANIFEST_VERSION,
        "id": _clean_str(meta.get("slug") or meta.get("id")),
        "title": _clean_str(meta.get("title")),
    }
    for src, dst in (("description", "description"), ("default_license", "default_license"),
                     ("cover_image", "cover_image")):
        val = _clean_str(meta.get(src))
        if val is not None:
            manifest[dst] = val
    pub: dict = {}
    for k in ("id", "name", "url"):
        val = _clean_str(publisher.get(k))
        if val is not None:
            pub[k] = val
    if pub:
        manifest["publisher"] = pub
    if generated_at:
        manifest["generated_at"] = generated_at
    manifest["items"] = [build_item(it) for it in items]
    return manifest


def assemble_and_validate(meta: dict, items: list[dict], *, generated_at: str | None = None
                          ) -> tuple[dict, list[str]]:
    """Build the manifest and validate it. Returns (manifest, errors); errors == [] means valid."""
    manifest = build_manifest(meta, items, generated_at=generated_at)
    return manifest, validate_manifest(manifest)


def assemble_validate_sign(meta: dict, items: list[dict], private_b64: str,
                           public_b64: str | None = None, *, generated_at: str | None = None
                           ) -> tuple[dict, list[str]]:
    """Build + validate, and sign ONLY if valid. Returns (manifest, errors). When errors is non-empty
    the manifest is the unsigned draft (so callers can surface what to fix); otherwise it's signed."""
    manifest, errors = assemble_and_validate(meta, items, generated_at=generated_at)
    if errors:
        return manifest, errors
    return sign_manifest(manifest, private_b64, public_b64), []


# ---------------------------------------------------------------- slug
def _slugify(text: str) -> str:
    """Lowercase, hyphenate, strip to [a-z0-9-]. Used for collection slugs and item ids."""
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "untitled"
