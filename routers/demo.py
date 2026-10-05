"""GET /api/demo — tells the frontend whether it's running in the public demo (core/demo.py) so
admin.js/app.js can adjust their own UI (banner, hidden nav, disabled mutation controls) without
duplicating the SD_DEMO_MODE env check client-side."""

from fastapi import APIRouter

import config

router = APIRouter()


@router.get("/api/demo")
async def get_demo_status():
    # `appliance` lets the admin skip /api/health/host (404 off-appliance) instead of logging it each load.
    if not config.DEMO_MODE:
        return {"demo": False, "appliance": config.IS_APPLIANCE}
    return {
        "demo": True,
        "appliance": config.IS_APPLIANCE,
        "repo_url": "https://github.com/pieria-art/Pieria",
        "releases_url": "https://github.com/pieria-art/Pieria/releases/latest",
    }
