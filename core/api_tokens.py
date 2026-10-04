"""API tokens for the public API (/api/v1) — mint, verify, list, revoke (ADR-147).

A token is `pieria_` + 32 random bytes (urlsafe base64). Only its sha256 is stored; the plaintext is
returned exactly once, by `mint_token`. Verification hashes the presented value, looks the hash up
(unique index), and confirms with a constant-time compare. Scopes are a subset of {read, control}:
`read` for GETs, `control` for commands/patches — they are independent (control does NOT imply read).
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from models import ApiTokenModel

TOKEN_PREFIX = "pieria_"
VALID_SCOPES = frozenset({"read", "control"})
#: last_used_at is a "roughly when" signal; writing it on every poll (HA polls every ~10 s) would turn
#: a read API into a write storm on SQLite, so it moves at most once per this interval.
LAST_USED_RESOLUTION = timedelta(minutes=1)
MAX_NAME_LEN = 64


class TokenError(ValueError):
    """Invalid mint request (bad name/scopes). The message is safe to show to the caller."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_scopes(scopes: Iterable[str]) -> list[str]:
    """Validate + dedupe + order a requested scope list. Raises TokenError."""
    requested = {str(s).strip().lower() for s in scopes}
    if not requested:
        raise TokenError("at least one scope is required")
    unknown = requested - VALID_SCOPES
    if unknown:
        raise TokenError(f"unknown scope(s): {', '.join(sorted(unknown))} (valid: read, control)")
    return sorted(requested)


def mint_token(db: Session, name: str, scopes: Iterable[str]) -> tuple[ApiTokenModel, str]:
    """Create a token. Returns (row, plaintext) — the plaintext is NOT recoverable afterwards."""
    name = (name or "").strip()
    if not name:
        raise TokenError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise TokenError(f"name must be at most {MAX_NAME_LEN} characters")
    scope_list = normalize_scopes(scopes)
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiTokenModel(name=name, token_hash=hash_token(plaintext), scopes=",".join(scope_list),
                        created_at=datetime.now(UTC))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, plaintext


def token_scopes(row: ApiTokenModel) -> list[str]:
    return [s for s in (row.scopes or "").split(",") if s]


def verify_token(db: Session, presented: str, now: Optional[datetime] = None) -> Optional[ApiTokenModel]:
    """The live (non-revoked) token row for a presented bearer value, or None."""
    if not presented or not presented.startswith(TOKEN_PREFIX) or len(presented) > 256:
        return None
    digest = hash_token(presented)
    row = db.query(ApiTokenModel).filter(ApiTokenModel.token_hash == digest).first()
    if row is None or not hmac.compare_digest(row.token_hash, digest):
        return None
    if row.revoked_at is not None:
        return None
    now = now or datetime.now(UTC)
    last = row.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if last is None or now - last >= LAST_USED_RESOLUTION:
        row.last_used_at = now
        db.commit()
    return row


def list_tokens(db: Session) -> list[ApiTokenModel]:
    return db.query(ApiTokenModel).order_by(ApiTokenModel.id).all()


def revoke_token(db: Session, token_id: int) -> Optional[ApiTokenModel]:
    """Mark revoked (idempotent — an already-revoked token keeps its original revoked_at).
    None if no such token."""
    row = db.query(ApiTokenModel).filter(ApiTokenModel.id == token_id).first()
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(row)
    return row
