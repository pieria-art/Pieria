# Samsung Frame TV push

Push **curated, open, no-subscription** Pieria art into a Samsung Frame TV's **Art Mode** over
your local network — turning a Frame you already own into another Pieria display, with no
Samsung account and no Art Store subscription.

## Two ways to use it

### 1. Built in to the server (recommended)
Pieria has a built-in Frame pusher. Configure it in **Admin → Settings → 🖼️ Frame TV**:

- **Enable** + your Frame's **IP address**
- a **playlist** to show, a **refresh interval**, and (optionally) resolution/matte
- hit **Test / Push now** to push immediately

The server then keeps the Frame updated on the interval, reusing the same curation brain (shuffle +
affinity) as every other display. The first push triggers an **"Allow this device?" prompt on the TV**
— accept it once; the pairing token is saved under the server's `data/` directory.

### 2. `push_once.py` (standalone, for testing / one-offs)
A dependency-light CLI for a single push — handy for confirming a Frame works before wiring up the
server loop, or for beta testers verifying on real hardware:

```bash
pip install httpx Pillow samsungtvws
python push_once.py --host 192.168.1.42 --server http://192.168.1.10:8000 --playlist "The Masterpieces"
```

Options: `--host` (TV IP, required), `--server`, `--playlist` (blank = server's first), `--port`
(default 8001), `--width`/`--height` (default 3840×2160), `--matte` (default `none`), `--token`
(pairing-token file).

## Requirements & notes

- A Samsung **Frame** TV (2017+ with Art Mode) reachable on your LAN; uses the unofficial art
  WebSocket API via [`samsungtvws`](https://github.com/xchwarze/samsung-tv-ws-api).
- The TV must allow unknown devices (first-connect prompt). If pushes silently fail, re-check the
  TV's network/permission settings and that the IP is correct.
- This is an **unofficial** integration; Samsung can change the protocol in a firmware update.

## Testing status (honest)

The render + push orchestration (dedupe, delete-old, error handling, scheduling) and the
`samsungtvws` API mapping are covered by automated tests against a fake TV. **The live TV handshake
is verified by running `push_once.py` against a real Frame** — if you have one and try it, please open
an issue with the result. See `tests/test_frame_push.py`.
