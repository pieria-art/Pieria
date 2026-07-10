"""Domain routers extracted from app.py (behavior-preserving split, ADR — refactor/app-split).

Import rule: routers/* may import from core/, config, models, database, and the app's domain
modules (federation, publisher, ai_client, frame_push, host_health, ...) — never from `app` itself.
`app.py` imports each router and calls `app.include_router(...)`.
"""
