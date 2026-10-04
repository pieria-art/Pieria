"""Internal management of public-API tokens (ADR-147) — what Admin -> API & Integrations calls.

NOT part of the v1 contract (never in /api/v1/openapi.json). Gated like the other secret-handling
surfaces: a trusted same-origin browser request, or the appliance token. One addition over a bare
`require_trusted_request`: browsers do not send an `Origin` header on a same-origin GET, so the
list route would 403 from the admin page itself; `Sec-Fetch-Site: same-origin` (set by the browser,
not overridable from page JS) is accepted as the same-origin signal for it. A plain docker-compose
install has no SD_APPLIANCE_UPDATE_TOKEN — the gate then admits same-origin browsers only, which is
exactly the admin GUI (non-browser LAN callers are refused; they have no business minting tokens).
"""

from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import api_tokens
from database import get_db
from routers.health import require_trusted_request

router = APIRouter()

_DETAIL = "managing API tokens requires a same-origin request or a valid token"


def _gate(request: Request, x_appliance_token: Optional[str]) -> None:
    if request.headers.get("sec-fetch-site", "").lower() == "same-origin":
        return
    require_trusted_request(request, x_appliance_token, detail=_DETAIL)


class TokenMintRequest(BaseModel):
    name: str
    scopes: list[str] = ["read"]


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=UTC))


def _view(row) -> dict:
    return {"id": row.id, "name": row.name, "scopes": api_tokens.token_scopes(row),
            "created_at": _utc(row.created_at), "last_used_at": _utc(row.last_used_at),
            "revoked_at": _utc(row.revoked_at)}


@router.get("/api/settings/api-tokens")
async def list_api_tokens(request: Request, x_appliance_token: Optional[str] = Header(None),
                          db: Session = Depends(get_db)):
    """Every token (including revoked) — metadata only. The secret is never retrievable."""
    _gate(request, x_appliance_token)
    return [_view(r) for r in api_tokens.list_tokens(db)]


@router.post("/api/settings/api-tokens", status_code=201)
async def mint_api_token(payload: TokenMintRequest, request: Request,
                         x_appliance_token: Optional[str] = Header(None),
                         db: Session = Depends(get_db)):
    """Mint a token. The plaintext `token` is in THIS response only — show it once, then it is gone."""
    _gate(request, x_appliance_token)
    try:
        row, plaintext = api_tokens.mint_token(db, payload.name, payload.scopes)
    except api_tokens.TokenError as e:
        raise HTTPException(400, detail=str(e))
    return {**_view(row), "token": plaintext}


@router.delete("/api/settings/api-tokens/{token_id}")
async def revoke_api_token(token_id: int, request: Request,
                           x_appliance_token: Optional[str] = Header(None),
                           db: Session = Depends(get_db)):
    """Revoke a token (it 401s immediately). Idempotent; the row stays so the list shows history."""
    _gate(request, x_appliance_token)
    row = api_tokens.revoke_token(db, token_id)
    if row is None:
        raise HTTPException(404, detail="no such token")
    return _view(row)
