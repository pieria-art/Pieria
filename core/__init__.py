"""Shared kernel for the Pieria app.

Cross-domain primitives that multiple routers (and the app lifespan) depend on:
connection registry, media/derivative rendering, the SSRF-safe downloader, playback
selection + now-playing helpers, security/origin checks, and settings utilities.

Import rule (prevents cycles): routers/* -> core/* -> (models, database, config,
domain modules). core/* must NOT import from routers/*.
"""
