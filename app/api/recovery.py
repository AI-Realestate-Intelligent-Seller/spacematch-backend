"""Account recovery: a verified recovery email, changing the login email, and recovering an account.

Logged in (Account page):
  - add / change the recovery email: current password, then a 6-digit code sent to that address
  - remove it: current password
  - change the login email: current password, then a code sent to the new address
Logged out ("Recover account", offered after 5 failed logins and on the forgot-password page):
  1. primary email + recovery email  -> a code goes to the recovery email, only if they match a verified pair
     (the answer is always the same, so nobody can learn an account's recovery email by guessing)
  2. the code                        -> a short recovery session (15 minutes, this browser only)
  3. a new password                  -> other sessions are logged out, alerts go to both addresses, logged in

Codes: 6 digits, valid 10 minutes, 5 wrong tries, newest only, single use, stored as an HMAC (never in clear).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import PasswordRequest, check_csrf, current_user, login_limiter, normalize_email, start_session
from app.core.mailer import send_email
from app.core.security import RateLimiter, hash_password, verify_password
from app.core.settings import BASE_URL, SESSION_SECRET
from app.db.storage import db, get_many, get_one, now, uid

router = APIRouter(prefix="/api")
CSRF = [Depends(check_csrf)]

CODE_TTL_S = 10 * 60
CODE_MAX_ATTEMPTS = 5
RESEND_AFTER_S = 60
CODES_PER_HOUR = 5
RECOVERY_SESSION_TTL_S = 15 * 60

VERIFY_RECOVERY, CHANGE_EMAIL, RECOVER = "verify_recovery", "change_email", "recover"
KINDS = {"recovery-email": VERIFY_RECOVERY, "email": CHANGE_EMAIL}  # URL name -> code purpose

recover_limiter = RateLimiter(limit=5, window=15 * 60)    # recovery requests per primary email / IP
code_limiter = RateLimiter(limit=20, window=15 * 60)      # wrong recovery codes per IP
password_limiter = RateLimiter(limit=10, window=15 * 60)  # wrong current passwords on the Account page

BAD_CODE = "That code is incorrect or has expired. Check the latest email or request a new code."
RECOVER_SENT = {"ok": True, "message": "If these details match a verified recovery email, we've sent a "
                                       "6-digit code to it. It expires in 10 minutes."}


# ------------------------------------------------------------- helpers ----

def mask_email(email: str | None) -> str:
    """o•••@g•••.com - enough to recognise your own address, not enough to learn someone else's."""
    name, _, domain = (email or "").partition("@")
    host, dot, tld = domain.rpartition(".")
    return f"{name[:1]}•••@{host[:1]}•••{dot}{tld}"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def log_event(c, user_id: str, event: str, request: Request | None = None, detail: str | None = None):
    c.execute("INSERT INTO account_events(id,user_id,event,detail,ip,created_at) VALUES(?,?,?,?,?,?)",
              (uid(), user_id, event, detail, client_ip(request) if request else None, now()))


def _code_hash(code_id: str, code: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), f"{code_id}:{code}".encode(), hashlib.sha256).hexdigest()


def send_wait(user_id: str, purpose: str) -> str | None:
    """Why another code can't be sent yet, or None."""
    t = int(time.time())
    sent = [r["sent_at"] for r in get_many("SELECT sent_at FROM email_codes WHERE user_id=? AND purpose=? AND sent_at>?",
                                           (user_id, purpose, t - 3600))]
    if sent and max(sent) > t - RESEND_AFTER_S:
        return "Please wait a minute before requesting another code"
    if len(sent) >= CODES_PER_HOUR:
        return "Too many codes requested - try again in an hour"
    return None


def issue_code(user_id: str, purpose: str, email: str) -> str:
    """New code for (user, purpose); any older one stops working."""
    code, code_id, t = f"{secrets.randbelow(10 ** 6):06d}", uid(), int(time.time())
    with db() as c:
        c.execute("UPDATE email_codes SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
                  (now(), user_id, purpose))
        c.execute("INSERT INTO email_codes(id,user_id,purpose,email,code_hash,attempts,expires_at,sent_at,created_at) "
                  "VALUES(?,?,?,?,?,0,?,?,?)",
                  (code_id, user_id, purpose, email, _code_hash(code_id, code), t + CODE_TTL_S, t, now()))
    return code


def active_code(user_id: str, purpose: str) -> dict | None:
    return get_one("SELECT * FROM email_codes WHERE user_id=? AND purpose=? AND used_at IS NULL AND expires_at>? "
                   "ORDER BY sent_at DESC LIMIT 1", (user_id, purpose, int(time.time())))


def use_code(user_id: str, purpose: str, code: str, generic: bool = False) -> dict:
    """Consume the active code when `code` matches; returns its row. `generic` hides why it failed."""
    row = active_code(user_id, purpose)
    if not row:
        raise HTTPException(400, BAD_CODE if generic else "This code has expired - request a new one")
    if not hmac.compare_digest(row["code_hash"], _code_hash(row["id"], code.strip())):
        tries = row["attempts"] + 1
        with db() as c:
            c.execute("UPDATE email_codes SET attempts=? WHERE id=?", (tries, row["id"]))
            if tries >= CODE_MAX_ATTEMPTS:
                c.execute("UPDATE email_codes SET used_at=? WHERE id=?", (now(), row["id"]))
        if generic:
            raise HTTPException(400, BAD_CODE)
        if tries >= CODE_MAX_ATTEMPTS:
            raise HTTPException(400, "Too many wrong codes - request a new code")
        left = CODE_MAX_ATTEMPTS - tries
        raise HTTPException(400, f"That code is incorrect - {left} {'try' if left == 1 else 'tries'} left")
    with db() as c:
        if c.execute("UPDATE email_codes SET used_at=? WHERE id=? AND used_at IS NULL", (now(), row["id"])).rowcount != 1:
            raise HTTPException(400, BAD_CODE if generic else "This code was already used - request a new one")
    return row


def require_password(user: dict, password: str):
    key = f"account:{user['id']}"
    if password_limiter.blocked(key):
        raise HTTPException(429, "Too many wrong passwords - try again in 15 minutes")
    if not verify_password(password, user.get("password_hash")):
        password_limiter.hit(key)
        raise HTTPException(400, "Your current password is incorrect")
    password_limiter.reset(key)


def fresh_user(user_id: str) -> dict:
    return get_one("SELECT * FROM users WHERE id=?", (user_id,))


def account_view(user: dict) -> dict:
    pending_recovery, pending_email = active_code(user["id"], VERIFY_RECOVERY), active_code(user["id"], CHANGE_EMAIL)
    verified = bool(user.get("recovery_email") and user.get("recovery_email_verified_at"))
    return {"name": user["name"], "email": user["email"], "role": user["role"],
            "recovery_email": user["recovery_email"] if verified else None,
            "recovery_email_verified_at": user.get("recovery_email_verified_at") if verified else None,
            "pending_recovery_email": pending_recovery["email"] if pending_recovery else None,
            "pending_login_email": pending_email["email"] if pending_email else None}


def code_email(to: str, name: str, purpose_line: str, code: str):
    send_email(to, f"Your SpaceMatch code: {code}",
               f"Hi {name},\n\n{purpose_line}\n\n    {code}\n\nThe code expires in 10 minutes and works once.\n"
               "If you didn't ask for this, ignore this email - nothing changes.\n")


def alert(to: str, name: str, subject: str, body: str):
    send_email(to, subject, f"Hi {name},\n\n{body}\n\nIf this wasn't you, reset your password now at "
                            f"{BASE_URL}/forgot-password\nor recover your account at {BASE_URL}/recover\n")


def same_domain(a: str, b: str) -> bool:
    return a.rpartition("@")[2] == b.rpartition("@")[2]


# --------------------------------------------------------- Account page ----

class EmailWithPassword(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PasswordOnly(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class CodeBody(BaseModel):
    code: str = Field(pattern=r"^\s*\d{6}\s*$")


@router.get("/account")
def get_account(user: dict = Depends(current_user)):
    return account_view(user)


@router.post("/account/recovery-email", dependencies=CSRF)
def set_recovery_email(body: EmailWithPassword, request: Request, user: dict = Depends(current_user)):
    """Add or change the recovery email. The current one keeps working until the new one is verified."""
    require_password(user, body.password)
    email = normalize_email(body.email)
    if email == user["email"]:
        raise HTTPException(422, "Use a different address from your login email")
    if email == user.get("recovery_email") and user.get("recovery_email_verified_at"):
        raise HTTPException(422, "This is already your recovery email")
    wait = send_wait(user["id"], VERIFY_RECOVERY)
    if wait:
        raise HTTPException(429, wait)
    code = issue_code(user["id"], VERIFY_RECOVERY, email)
    code_email(email, user["name"], "Use this code to confirm this address as the recovery email for your "
                                    "SpaceMatch account:", code)
    return {**account_view(user), "same_domain": same_domain(email, user["email"])}


@router.post("/account/recovery-email/verify", dependencies=CSRF)
def verify_recovery_email(body: CodeBody, request: Request, user: dict = Depends(current_user)):
    row = use_code(user["id"], VERIFY_RECOVERY, body.code)
    old = user.get("recovery_email") if user.get("recovery_email_verified_at") else None
    with db() as c:
        c.execute("UPDATE users SET recovery_email=?, recovery_email_verified_at=? WHERE id=?",
                  (row["email"], now(), user["id"]))
        log_event(c, user["id"], "recovery_email_verified", request, mask_email(row["email"]))
    alert(user["email"], user["name"], "Recovery email " + ("changed" if old else "added"),
          f"{mask_email(row['email'])} is now the recovery email for your SpaceMatch account.")
    if old and old != row["email"]:
        alert(old, user["name"], "You're no longer a SpaceMatch recovery email",
              f"This address is no longer the recovery email for the SpaceMatch account {mask_email(user['email'])}.")
    return account_view(fresh_user(user["id"]))


@router.post("/account/recovery-email/remove", dependencies=CSRF)
def remove_recovery_email(body: PasswordOnly, request: Request, user: dict = Depends(current_user)):
    require_password(user, body.password)
    old = user.get("recovery_email")
    if not old:
        raise HTTPException(404, "There is no recovery email to remove")
    with db() as c:
        c.execute("UPDATE users SET recovery_email=NULL, recovery_email_verified_at=NULL WHERE id=?", (user["id"],))
        log_event(c, user["id"], "recovery_email_removed", request, mask_email(old))
    alert(user["email"], user["name"], "Recovery email removed",
          f"{mask_email(old)} was removed as the recovery email for your SpaceMatch account.")
    if user.get("recovery_email_verified_at"):
        alert(old, user["name"], "You're no longer a SpaceMatch recovery email",
              f"This address is no longer the recovery email for the SpaceMatch account {mask_email(user['email'])}.")
    return account_view(fresh_user(user["id"]))


@router.post("/account/email", dependencies=CSRF)
def change_login_email(body: EmailWithPassword, request: Request, user: dict = Depends(current_user)):
    """Start changing the login email: a code goes to the new address."""
    require_password(user, body.password)
    email = normalize_email(body.email)
    if email == user["email"]:
        raise HTTPException(422, "This is already your login email")
    if email == user.get("recovery_email"):
        raise HTTPException(422, "That's your recovery email - remove it first or use another address")
    if get_one("SELECT id FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "Another account already uses this email")
    wait = send_wait(user["id"], CHANGE_EMAIL)
    if wait:
        raise HTTPException(429, wait)
    code = issue_code(user["id"], CHANGE_EMAIL, email)
    code_email(email, user["name"], "Use this code to make this address the login email of your SpaceMatch account:",
               code)
    return account_view(user)


@router.post("/account/email/verify", dependencies=CSRF)
def verify_login_email(body: CodeBody, request: Request, user: dict = Depends(current_user)):
    row = use_code(user["id"], CHANGE_EMAIL, body.code)
    if get_one("SELECT id FROM users WHERE email=? AND id<>?", (row["email"], user["id"])):
        raise HTTPException(409, "Another account already uses this email")
    old = user["email"]
    with db() as c:
        c.execute("UPDATE users SET email=? WHERE id=?", (row["email"], user["id"]))
        c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (now(), user["id"]))
        log_event(c, user["id"], "login_email_changed", request, f"{mask_email(old)} -> {mask_email(row['email'])}")
    alert(old, user["name"], "Your SpaceMatch login email was changed",
          f"Your SpaceMatch login email was changed from this address to {mask_email(row['email'])}.")
    updated = fresh_user(user["id"])
    return {**account_view(updated), "user": {k: updated[k] for k in ("id", "email", "name", "role", "brokerage")}}


@router.post("/account/{kind}/resend", dependencies=CSRF)
def resend_code(kind: str, user: dict = Depends(current_user)):
    purpose = KINDS.get(kind)
    if not purpose:
        raise HTTPException(404, "Not found")
    pending = active_code(user["id"], purpose)
    if not pending:
        raise HTTPException(404, "Nothing to resend - enter the email address again")
    wait = send_wait(user["id"], purpose)
    if wait:
        raise HTTPException(429, wait)
    code = issue_code(user["id"], purpose, pending["email"])
    line = ("Use this code to confirm this address as the recovery email for your SpaceMatch account:"
            if purpose == VERIFY_RECOVERY else
            "Use this code to make this address the login email of your SpaceMatch account:")
    code_email(pending["email"], user["name"], line, code)
    return account_view(user)


@router.post("/account/{kind}/cancel", dependencies=CSRF)
def cancel_code(kind: str, user: dict = Depends(current_user)):
    purpose = KINDS.get(kind)
    if not purpose:
        raise HTTPException(404, "Not found")
    with db() as c:
        c.execute("UPDATE email_codes SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
                  (now(), user["id"], purpose))
    return account_view(user)


# ------------------------------------------------------ Recover account ----

class RecoverRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    recovery_email: str = Field(min_length=3, max_length=254)


class RecoverCode(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(pattern=r"^\s*\d{6}\s*$")


@router.post("/auth/recover", dependencies=CSRF)
def request_recovery(body: RecoverRequest, request: Request):
    """Same answer whatever happens; a code is sent only for a matching, verified recovery email."""
    email = (body.email or "").strip().lower()
    recovery = (body.recovery_email or "").strip().lower()
    keys = (f"recover:{email}", f"ip:{client_ip(request)}")
    if recover_limiter.blocked(*keys):
        raise HTTPException(429, "Too many recovery requests - try again in 15 minutes")
    recover_limiter.hit(*keys)
    user = get_one("SELECT * FROM users WHERE email=?", (email,))
    matches = bool(user and user.get("password_hash") and user.get("recovery_email_verified_at")
                   and hmac.compare_digest((user.get("recovery_email") or "").encode(), recovery.encode()))
    if matches and not send_wait(user["id"], RECOVER):
        code = issue_code(user["id"], RECOVER, user["recovery_email"])
        code_email(user["recovery_email"], user["name"],
                   f"Someone (hopefully you) asked to recover the SpaceMatch account {mask_email(email)}.\n"
                   "Enter this code on the Recover account page:", code)
        with db() as c:
            log_event(c, user["id"], "recovery_requested", request)
    return RECOVER_SENT


@router.post("/auth/recover/verify", dependencies=CSRF)
def verify_recovery(body: RecoverCode, request: Request):
    """A correct code opens a 15-minute recovery session in this browser (to choose a new password)."""
    key = f"ip:{client_ip(request)}"
    if code_limiter.blocked(key):
        raise HTTPException(429, "Too many wrong codes - try again in 15 minutes")
    user = get_one("SELECT * FROM users WHERE email=?", ((body.email or "").strip().lower(),))
    try:
        if not user or not user.get("password_hash"):
            raise HTTPException(400, BAD_CODE)
        use_code(user["id"], RECOVER, body.code, generic=True)
    except HTTPException:
        code_limiter.hit(key)
        raise
    request.session["recovery"] = {"user_id": user["id"], "exp": int(time.time()) + RECOVERY_SESSION_TTL_S}
    return {"ok": True}


@router.post("/auth/recover/password", dependencies=CSRF)
def recover_set_password(body: PasswordRequest, request: Request):
    """New password, all other sessions logged out, alerts to both addresses, and this browser logged in."""
    pending = request.session.get("recovery") or {}
    user = fresh_user(pending["user_id"]) if pending.get("exp", 0) > time.time() else None
    if not user or not user.get("password_hash"):
        request.session.pop("recovery", None)
        raise HTTPException(400, "Your recovery session has expired - start again")
    if verify_password(body.password, user["password_hash"]):
        raise HTTPException(422, "Your new password must be different from your current password")
    changed = now()
    with db() as c:
        c.execute("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
                  (hash_password(body.password), changed, user["id"]))
        c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (changed, user["id"]))
        c.execute("UPDATE email_codes SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
                  (changed, user["id"], RECOVER))
        log_event(c, user["id"], "recovered_with_recovery_email", request)
    login_limiter.reset(f"email:{user['email']}")
    body_text = ("Your SpaceMatch account was recovered using its recovery email and a new password was set. "
                 "All other devices were logged out.")
    alert(user["email"], user["name"], "Your SpaceMatch account was recovered", body_text)
    if user.get("recovery_email"):
        alert(user["recovery_email"], user["name"], "Your SpaceMatch account was recovered", body_text)
    return start_session(request, fresh_user(user["id"]))  # also ends the recovery session
