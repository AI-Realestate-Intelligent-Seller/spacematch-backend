"""API keys for the Hub (server-to-server): `Authorization: Bearer sk_live_...`.

A key belongs to one agent and can reach only that agent's properties.
Agents create, rotate and revoke keys from the dashboard (cookie session, routes under /api).
Only a SHA-256 hash of the secret is stored; the secret is shown once.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.auth import check_csrf, require_agent
from app.core.settings import API_RATE_LIMIT_PER_MIN
from app.db.storage import db, get_many, get_one, now, uid

SCOPES = ("properties:read", "properties:write", "socials:read", "socials:write", "refresh:write", "logs:read")


class ApiError(Exception):
    """Error rendered as {"error": {"code", "message", "field"}} on /v1 routes."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field, self.headers = status, code, message, field, headers


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _utc(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _create_key(owner_id: str, name: str, scopes: list[str], environment: str, expires_at: str | None) -> tuple[dict, str]:
    secret = f"sk_{'live' if environment == 'production' else 'test'}_{secrets.token_urlsafe(32)}"
    row = {"id": "key_" + uid().replace("-", "")[:16], "owner_id": owner_id, "name": name, "prefix": secret[:12],
           "secret_hash": _hash(secret), "scopes": " ".join(scopes), "environment": environment,
           "created_at": now(), "expires_at": expires_at}
    with db() as c:
        c.execute("INSERT INTO api_keys(id,owner_id,name,prefix,secret_hash,scopes,environment,created_at,expires_at) "
                  "VALUES(:id,:owner_id,:name,:prefix,:secret_hash,:scopes,:environment,:created_at,:expires_at)", row)
    return get_one("SELECT * FROM api_keys WHERE id=?", (row["id"],)), secret


def key_object(k: dict) -> dict:
    expires = _utc(k["expires_at"])
    state = "revoked" if k["revoked_at"] else ("expired" if expires and expires <= datetime.now(timezone.utc) else "active")
    return {"id": k["id"], "name": k["name"], "prefix": k["prefix"] + "…", "scopes": k["scopes"].split(),
            "environment": k["environment"], "status": state, "created_at": k["created_at"],
            "last_used_at": k["last_used_at"], "expires_at": k["expires_at"], "revoked_at": k["revoked_at"]}


# -------------------------------------------------------- request auth ----

class _Window:
    """Fixed one-minute window per key."""

    def __init__(self):
        self.lock, self.windows = threading.Lock(), {}

    def hit(self, key_id: str, limit: int) -> tuple[int, int, bool]:
        with self.lock:
            minute = int(time.time() // 60) * 60
            start, count = self.windows.get(key_id, (minute, 0))
            if start != minute:
                start, count = minute, 0
            count += 1
            self.windows[key_id] = (start, count)
            return max(0, limit - count), start + 60, count > limit


rate_window = _Window()


def require_scope(scope: str):
    """Dependency: valid, unexpired, unrevoked API key with `scope`. Returns the key row."""

    def dependency(request: Request, response: Response, authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ApiError(401, "unauthorized", "Send your API key as 'Authorization: Bearer sk_…'")
        secret = authorization[7:].strip()
        if not secret or "{{" in secret:  # e.g. Postman sent an unset {{apiKey}} variable
            raise ApiError(401, "unauthorized", "No API key was sent (the Authorization header is empty or an unset "
                                                "variable like {{apiKey}}). Create a key and send it as 'Bearer sk_…'")
        key = get_one("SELECT * FROM api_keys WHERE secret_hash=?", (_hash(secret),))
        expires = _utc(key["expires_at"]) if key else None
        if not key or key["revoked_at"] or (expires and expires <= datetime.now(timezone.utc)):
            raise ApiError(401, "unauthorized", "This API key is invalid, revoked or expired")
        remaining, reset, over = rate_window.hit(key["id"], API_RATE_LIMIT_PER_MIN)
        headers = {"X-RateLimit-Limit": str(API_RATE_LIMIT_PER_MIN), "X-RateLimit-Remaining": str(remaining),
                   "X-RateLimit-Reset": str(reset)}
        if over:
            raise ApiError(429, "rate_limited", "Too many requests for this API key",
                           headers={**headers, "Retry-After": str(max(1, reset - int(time.time())))})
        response.headers.update(headers)
        if scope not in key["scopes"].split():
            raise ApiError(403, "forbidden", f"This API key is missing the '{scope}' scope")
        last = _utc(key["last_used_at"])
        if not last or datetime.now(timezone.utc) - last > timedelta(minutes=1):
            with db() as c:
                c.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now(), key["id"]))
        request.state.api_key = key
        return key

    return dependency


# ------------------------------------------------ dashboard management ----

router = APIRouter(prefix="/api/api-keys", tags=["dashboard: API keys"])


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    scopes: list[Literal[SCOPES]] = Field(default_factory=lambda: list(SCOPES), min_length=1)  # type: ignore[valid-type]
    environment: Literal["production", "preview"] = "production"
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class RotateRequest(BaseModel):
    grace_hours: int = Field(default=24, ge=0, le=720)


def _owned_key(owner: dict, key_id: str) -> dict:
    key = get_one("SELECT * FROM api_keys WHERE id=? AND owner_id=?", (key_id, owner["id"]))
    if not key:
        raise HTTPException(404, "API key not found")
    return key


@router.get("")
def list_keys(owner: dict = Depends(require_agent)):
    return [key_object(k) for k in get_many("SELECT * FROM api_keys WHERE owner_id=? ORDER BY created_at DESC",
                                            (owner["id"],))]


@router.post("", dependencies=[Depends(check_csrf)])
def create_key(body: CreateKeyRequest, owner: dict = Depends(require_agent)):
    expires = None
    if body.expires_in_days:
        expires = (datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)).isoformat(timespec="seconds")
    key, secret = _create_key(owner["id"], body.name.strip(), list(dict.fromkeys(body.scopes)), body.environment, expires)
    return {**key_object(key), "secret": secret}


@router.post("/{key_id}/rotate", dependencies=[Depends(check_csrf)])
def rotate_key(key_id: str, body: RotateRequest | None = None, owner: dict = Depends(require_agent)):
    old = _owned_key(owner, key_id)
    if old["revoked_at"]:
        raise HTTPException(409, "This key is revoked")
    grace = (body or RotateRequest()).grace_hours
    key, secret = _create_key(owner["id"], old["name"], old["scopes"].split(), old["environment"], old["expires_at"])
    old_until = datetime.now(timezone.utc) + timedelta(hours=grace)
    current = _utc(old["expires_at"])
    with db() as c:
        if grace == 0:
            c.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (now(), old["id"]))
        elif not current or old_until < current:
            c.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (old_until.isoformat(timespec="seconds"), old["id"]))
    return {**key_object(key), "secret": secret, "previous_key_expires_at":
            None if grace == 0 else old_until.isoformat(timespec="seconds")}


@router.delete("/{key_id}", dependencies=[Depends(check_csrf)])
def revoke_key(key_id: str, owner: dict = Depends(require_agent)):
    key = _owned_key(owner, key_id)
    if not key["revoked_at"]:
        with db() as c:
            c.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (now(), key_id))
    return {"ok": True}
