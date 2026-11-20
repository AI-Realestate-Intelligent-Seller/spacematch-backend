"""Sessions, CSRF, sign-up / login / logout and broker invite acceptance.

Roles:
  agent  - signs up, creates and edits properties, manages API keys (and can invite brokers).
  broker - invited by an agent by email; sees only properties shared with them (to be extended later).
"""
from __future__ import annotations

import hmac
import re
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.mailer import send_email
from app.core.security import DUMMY_HASH, RateLimiter, hash_password, hash_token, verify_password
from app.core.settings import BASE_URL
from app.db.storage import adopt_legacy_data, db, get_one, now, uid

router = APIRouter(prefix="/api")
login_limiter = RateLimiter(limit=10, window=15 * 60)
reset_limiter = RateLimiter(limit=5, window=15 * 60)  # reset emails per email / IP
RESET_TTL_S = 60 * 60  # a reset link works for 1 hour, once
FAILED_ATTEMPTS_HEADER = "X-Failed-Attempts"

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}$")
PUBLIC_USER_FIELDS = ("id", "email", "name", "role", "brokerage")


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    return email


def public_user(user: dict | None) -> dict | None:
    return {k: user.get(k) for k in PUBLIC_USER_FIELDS} if user else None


# ------------------------------------------------------------ sessions ----

def ensure_csrf(request: Request) -> str:
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def check_csrf(request: Request, x_csrf_token: str | None = Header(default=None)):
    """Dependency for every state-changing route (login included)."""
    expected = ensure_csrf(request)
    if not x_csrf_token or not hmac.compare_digest(x_csrf_token, expected):
        raise HTTPException(403, "CSRF token mismatch - reload the page")


def session_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = get_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not user or not user.get("password_hash"):
        request.session.pop("user_id", None)
        return None
    if (user.get("password_changed_at") or "") != request.session.get("pwd_at", ""):
        request.session.clear()  # the password changed since this session started: log it out
        return None
    return user


def current_user(request: Request) -> dict:
    user = session_user(request)
    if not user:
        raise HTTPException(401, "Please log in")
    return user


def require_agent(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "agent":
        raise HTTPException(403, "Only agents can do this")
    return user


def start_session(request: Request, user: dict):
    """New session on login (prevents session fixation) with a fresh CSRF token."""
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["pwd_at"] = user.get("password_changed_at") or ""
    with db() as c:
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user["id"]))
    return {"user": public_user(user), "csrf": ensure_csrf(request)}


# -------------------------------------------------------------- routes ----

class SignupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


@router.post("/auth/signup", dependencies=[Depends(check_csrf)])
def signup(body: SignupRequest, request: Request):
    """Self sign-up creates an agent. Brokers join through an agent's invite."""
    email = normalize_email(body.email)
    if get_one("SELECT id FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "An account with this email already exists - log in instead")
    first_agent = not get_one("SELECT id FROM users WHERE role='agent' LIMIT 1")
    user = {"id": uid(), "email": email, "name": body.name.strip(), "role": "agent", "brokerage": None,
            "password_hash": hash_password(body.password), "created_at": now()}
    with db() as c:
        c.execute("INSERT INTO users(id,email,name,role,brokerage,password_hash,created_at) "
                  "VALUES(:id,:email,:name,:role,:brokerage,:password_hash,:created_at)", user)
    if first_agent:
        adopt_legacy_data(user["id"])
    return start_session(request, user)


@router.post("/auth/login", dependencies=[Depends(check_csrf)])
def login(body: LoginRequest, request: Request):
    email = (body.email or "").strip().lower()
    ip = request.client.host if request.client else "unknown"
    keys = (f"email:{email}", f"ip:{ip}")
    if login_limiter.blocked(*keys):
        raise HTTPException(429, "Too many failed attempts - try again in 15 minutes",
                            headers={FAILED_ATTEMPTS_HEADER: str(login_limiter.count(keys[0]))})
    user = get_one("SELECT * FROM users WHERE email=?", (email,))
    if not verify_password(body.password, (user or {}).get("password_hash") or DUMMY_HASH) or not user:
        login_limiter.hit(*keys)
        # Counted per email whether or not it has an account; the page offers "Recover account" after 5.
        raise HTTPException(401, "Incorrect email or password",
                            headers={FAILED_ATTEMPTS_HEADER: str(login_limiter.count(keys[0]))})
    login_limiter.reset(f"email:{email}")
    return start_session(request, user)


@router.post("/auth/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True, "csrf": ensure_csrf(request)}


def valid_invite(token: str) -> dict:
    invite = get_one("SELECT i.*, u.email, u.name, u.brokerage, u.password_hash FROM invites i "
                     "JOIN users u ON u.id=i.user_id WHERE i.token_hash=?", (hash_token(token),))
    if not invite or invite["used_at"] or invite["expires_at"] < time.time() or invite["password_hash"]:
        raise HTTPException(404, "This invite link is invalid or has expired - ask the agent to resend it")
    return invite


@router.get("/invites/{token}")
def invite_details(token: str):
    invite = valid_invite(token)
    owner = get_one("SELECT name FROM users WHERE id=?", (invite["invited_by"],)) or {}
    return {"email": invite["email"], "name": invite["name"], "brokerage": invite["brokerage"],
            "invited_by": owner.get("name")}


@router.post("/invites/{token}/accept", dependencies=[Depends(check_csrf)])
def accept_invite(token: str, body: PasswordRequest, request: Request):
    invite = valid_invite(token)
    with db() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash IS NULL",
                  (hash_password(body.password), invite["user_id"]))
        c.execute("UPDATE invites SET used_at=? WHERE user_id=? AND used_at IS NULL", (now(), invite["user_id"]))
    return start_session(request, get_one("SELECT * FROM users WHERE id=?", (invite["user_id"],)))


# ------------------------------------------------------ password reset ----

class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


RESET_SENT = {"ok": True, "message": "If an account exists for that email, we've sent a link to reset the password."}


@router.post("/auth/forgot-password", dependencies=[Depends(check_csrf)])
def forgot_password(body: ForgotPasswordRequest, request: Request):
    """Emails a one-time reset link. Answers the same whether or not the email has an account."""
    email = (body.email or "").strip().lower()
    ip = request.client.host if request.client else "unknown"
    keys = (f"email:{email}", f"ip:{ip}")
    if reset_limiter.blocked(*keys):
        raise HTTPException(429, "Too many reset requests - try again in 15 minutes")
    reset_limiter.hit(*keys)
    user = get_one("SELECT * FROM users WHERE email=?", (email,))
    if not user or not user.get("password_hash"):  # unknown email or a broker who hasn't accepted the invite
        return RESET_SENT
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (now(), user["id"]))
        c.execute("INSERT INTO password_resets(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                  (hash_token(token), user["id"], int(time.time()) + RESET_TTL_S, now()))
    link = f"{BASE_URL}/reset-password/{token}"
    send_email(user["email"], "Reset your SpaceMatch password",
               f"Hi {user['name']},\n\nSomeone (hopefully you) asked to reset your SpaceMatch password.\n"
               f"Choose a new password here (the link works once, for 1 hour):\n\n{link}\n\n"
               "If you didn't ask for this, ignore this email - your password stays the same.\n")
    return RESET_SENT


def valid_reset(token: str) -> dict:
    row = get_one("SELECT r.*, u.email, u.password_hash FROM password_resets r JOIN users u ON u.id=r.user_id "
                  "WHERE r.token_hash=?", (hash_token(token),))
    if not row or row["used_at"] or row["expires_at"] < time.time() or not row["password_hash"]:
        raise HTTPException(404, "This reset link is invalid or has expired - request a new one")
    return row


@router.get("/password-resets/{token}")
def reset_details(token: str):
    return {"email": valid_reset(token)["email"]}


@router.post("/password-resets/{token}", dependencies=[Depends(check_csrf)])
def reset_password(token: str, body: PasswordRequest, request: Request):
    """Sets a new password (must differ from the current one), logs out other sessions, logs this one in."""
    reset = valid_reset(token)
    if verify_password(body.password, reset["password_hash"]):
        raise HTTPException(422, "Your new password must be different from your current password")
    changed = now()
    with db() as c:
        c.execute("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
                  (hash_password(body.password), changed, reset["user_id"]))
        c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (changed, reset["user_id"]))
    login_limiter.reset(f"email:{reset['email']}")
    return start_session(request, get_one("SELECT * FROM users WHERE id=?", (reset["user_id"],)))
