"""Origin/CORS trust checks shared by the CORS middleware, the appliance-update route, and the
`/ws` handshake.

The app is a no-login LAN kiosk (ADR-013/015): the trust boundary is "you are a device on my
LAN". These helpers decide whether a request's `Origin` header should be treated as trusted
(same-origin, or explicitly configured via `SD_ALLOWED_ORIGINS`).
"""

import config

# The read-only public FEED (what integrations consume) stays cross-origin readable even when the
# Origin isn't otherwise trusted — shared by the CORS middleware's is_public_read check.
_PUBLIC_FEED_GET_PREFIXES = ("/next-image", "/api/catalog", "/display/")


def _same_origin(origin: str, host: str) -> bool:
    """True when the request Origin points back at the same host:port the request was addressed to
    (the kiosk loading its own page). Origin is `scheme://host[:port]`; Host is `host[:port]`."""
    sep = origin.find("://")
    return bool(origin) and bool(host) and sep != -1 and origin[sep + 3:] == host


def _origin_allowed(origin: str, host: str) -> bool:
    return origin in config.ALLOWED_ORIGINS or _same_origin(origin, host)
