#!/usr/bin/env python3
"""
push_once.py — one-shot Pieria → Samsung Frame TV push (real-hardware test tool).

Pieria's built-in Frame pusher is exercised end-to-end in CI against a fake client, but the
actual Samsung art-mode WebSocket handshake can only be confirmed on a real Frame. This standalone
script is how a Frame owner (or beta tester) does that confirmation: it pulls the current artwork from
a running Pieria server over HTTP, renders it to the TV's resolution, and pushes it into Art
Mode — printing each step verbosely.

Standalone on purpose (only needs: httpx, Pillow, samsungtvws) so a tester can run it without the
full app environment:

    pip install httpx Pillow samsungtvws
    python push_once.py --host 192.168.1.42 --server http://192.168.1.10:8000 --playlist "The Masterpieces"

On the FIRST connection the TV shows an "Allow this device?" prompt — accept it; the pairing token is
saved to --token so later runs are silent.
"""

import argparse
import io
import sys

import httpx
from PIL import Image, ImageOps


def fetch_current(server: str, playlist: str, display_id: str):
    """Ask the Pieria server for the current artwork; return (image_bytes, title)."""
    params = {"playlist_name": playlist, "display_id": display_id, "direction": "1"}
    with httpx.Client(timeout=30.0) as client:
        if not playlist:
            lists = client.get(f"{server}/playlists").json()
            if not lists:
                raise SystemExit("No playlists on the server.")
            params["playlist_name"] = lists[0]["name"]
            print(f"  (no playlist given; using '{params['playlist_name']}')")
        info = client.get(f"{server}/next-image", params=params).json()
        img_url = info["image_url"]
        full = img_url if img_url.startswith("http") else server.rstrip("/") + img_url
        title = (info.get("metadata") or {}).get("title", "?")
        data = client.get(full).content
        return data, title


def render(data: bytes, w: int, h: int, quality: int = 90) -> bytes:
    """EXIF-orient + cover-crop to w x h, full-colour JPEG (mirrors epaper.render_fullcolor)."""
    with Image.open(io.BytesIO(data)) as src:
        img = ImageOps.exif_transpose(src).convert("RGB")
        fitted = ImageOps.fit(img, (w, h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        buf = io.BytesIO()
        fitted.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="Push the current Pieria artwork to a Samsung Frame TV.")
    ap.add_argument("--host", required=True, help="Frame TV IP/hostname")
    ap.add_argument("--server", default="http://localhost:8000", help="Pieria server URL")
    ap.add_argument("--playlist", default="", help="Playlist name (blank = server's first)")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--width", type=int, default=3840)
    ap.add_argument("--height", type=int, default=2160)
    ap.add_argument("--matte", default="none")
    ap.add_argument("--display-id", default="frame-tv-cli")
    ap.add_argument("--token", default="frame_tv_token.json", help="pairing-token file")
    args = ap.parse_args()

    try:
        from samsungtvws import SamsungTVWS
    except ImportError:
        raise SystemExit("samsungtvws not installed. Run: pip install samsungtvws")

    print(f"[1/4] Fetching current artwork from {args.server} ...")
    data, title = fetch_current(args.server, args.playlist, args.display_id)
    print(f"      got: {title} ({len(data)} bytes)")

    print(f"[2/4] Rendering to {args.width}x{args.height} ...")
    jpg = render(data, args.width, args.height)
    print(f"      rendered JPEG: {len(jpg)} bytes")

    print(f"[3/4] Connecting to Frame at {args.host}:{args.port} "
          f"(accept the on-TV prompt on first run) ...")
    tv = SamsungTVWS(host=args.host, port=args.port, token_file=args.token)
    art = tv.art()

    print("[4/4] Uploading + selecting + enabling Art Mode ...")
    content_id = art.upload(jpg, file_type="jpg", matte=args.matte)
    art.select_image(content_id, show=True)
    art.set_artmode(True)
    print(f"      ✓ pushed as content_id={content_id}. Check your Frame.")


if __name__ == "__main__":
    sys.exit(main())
