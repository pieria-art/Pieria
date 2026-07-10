"""Static-page GETs — the admin/help/studio/remote SPA shells + the retired /publisher redirect."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

from config import STATIC_DIR

router = APIRouter()


@router.get("/admin")
async def get_admin_page(): return FileResponse(STATIC_DIR / "admin.html")


@router.get("/help")
async def get_help_page(): return FileResponse(STATIC_DIR / "help.html")


@router.get("/studio")
async def get_studio_page(): return FileResponse(STATIC_DIR / "studio.html")


@router.get("/remote")
async def get_remote_page():
    return FileResponse(STATIC_DIR / "remote.html")


@router.get("/publisher")
async def get_publisher_page():
    # Publisher Studio is now a view inside the admin SPA; keep this path working for bookmarks/links.
    return RedirectResponse(url="/admin?view=publisher")
