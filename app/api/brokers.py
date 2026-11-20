"""Agent-side broker management: invite brokers to properties by email and set their access."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.access import require_property
from app.api.auth import check_csrf, normalize_email, require_agent
from app.core.mailer import email_configured, send_email
from app.core.security import hash_token, new_token
from app.core.settings import BASE_URL, INVITE_TTL_HOURS
from app.db.storage import db, get_many, get_one, now, uid

router = APIRouter(prefix="/api")
Access = Literal["view", "full"]
ACCESS_LABEL = {"view": "view only", "full": "full access"}


class InviteRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    name: str = Field(default="", max_length=120)
    brokerage: str = Field(default="", max_length=160)
    property_id: str = Field(min_length=1, max_length=64)
    access: Access = "view"


class AccessRequest(BaseModel):
    access: Access


def issue_invite(broker: dict, owner: dict) -> str:
    token = new_token()
    with db() as c:
        c.execute("INSERT INTO invites(token_hash,user_id,invited_by,expires_at,created_at) VALUES(?,?,?,?,?)",
                  (hash_token(token), broker["id"], owner["id"], int(time.time()) + INVITE_TTL_HOURS * 3600, now()))
    return f"{BASE_URL}/invite/{token}"


def send_invite_email(broker: dict, owner: dict, address: str, access: str, link: str | None) -> bool:
    if link:
        subject = f"{owner['name']} invited you to SpaceMatch"
        text = (f"Hi {broker['name']},\n\n{owner['name']} has invited you to SpaceMatch as a broker for\n"
                f"  {address} ({ACCESS_LABEL[access]}).\n\nSet your password to get started:\n{link}\n\n"
                f"This link expires in {INVITE_TTL_HOURS} hours.")
    else:
        subject = f"{owner['name']} shared a property with you on SpaceMatch"
        text = (f"Hi {broker['name']},\n\n{owner['name']} gave you {ACCESS_LABEL[access]} to\n  {address}\n\n"
                f"Log in to see it: {BASE_URL}/login")
    try:
        return send_email(broker["email"], subject, text)
    except Exception as exc:  # the access is granted even if the email fails
        print(f"[email failed] {type(exc).__name__}: {exc}", flush=True)
        return False


@router.get("/brokers")
def list_brokers(owner: dict = Depends(require_agent)):
    rows = get_many("""SELECT u.id, u.name, u.email, u.brokerage, u.password_hash IS NOT NULL AS active,
                       pb.property_id, pb.access, pb.created_at AS shared_at, p.address
                       FROM property_brokers pb JOIN users u ON u.id=pb.broker_id
                       JOIN properties p ON p.id=pb.property_id
                       WHERE p.owner_id=? ORDER BY u.name, p.address""", (owner["id"],))
    brokers: dict[str, dict] = {}
    for r in rows:
        b = brokers.setdefault(r["id"], {"id": r["id"], "name": r["name"], "email": r["email"],
                                         "brokerage": r["brokerage"], "active": bool(r["active"]),
                                         "properties": []})
        b["properties"].append({"property_id": r["property_id"], "address": r["address"],
                                "access": r["access"], "shared_at": r["shared_at"]})
    return list(brokers.values())


@router.post("/brokers/invite", dependencies=[Depends(check_csrf)])
def invite_broker(body: InviteRequest, owner: dict = Depends(require_agent)):
    prop, _ = require_property(owner, body.property_id, "agent")
    email = normalize_email(body.email)
    broker = get_one("SELECT * FROM users WHERE email=?", (email,))
    if broker and broker["role"] != "broker":
        raise HTTPException(409, "This email belongs to an agent account and can't be added as a broker")
    if not broker:
        name = body.name.strip()
        if len(name) < 2:
            raise HTTPException(422, "Enter the broker's name")
        broker = {"id": uid(), "email": email, "name": name, "role": "broker",
                  "brokerage": body.brokerage.strip() or None, "created_at": now()}
        with db() as c:
            c.execute("INSERT INTO users(id,email,name,role,brokerage,created_at) "
                      "VALUES(:id,:email,:name,:role,:brokerage,:created_at)", broker)
        broker["password_hash"] = None
    with db() as c:
        c.execute("""INSERT INTO property_brokers(property_id,broker_id,access,invited_by,created_at)
                     VALUES(?,?,?,?,?) ON CONFLICT(property_id,broker_id) DO UPDATE SET access=excluded.access""",
                  (prop["id"], broker["id"], body.access, owner["id"], now()))
    link = None if broker.get("password_hash") else issue_invite(broker, owner)
    emailed = send_invite_email(broker, owner, prop["address"], body.access, link)
    result = {"ok": True, "broker_id": broker["id"], "pending": link is not None, "emailed": emailed}
    if link and not email_configured():
        result["invite_link"] = link  # local setup without SMTP: let the agent copy the link
    return result


@router.post("/brokers/{broker_id}/resend", dependencies=[Depends(check_csrf)])
def resend_invite(broker_id: str, owner: dict = Depends(require_agent)):
    share = get_one("""SELECT p.address, pb.access FROM property_brokers pb JOIN properties p ON p.id=pb.property_id
                       WHERE pb.broker_id=? AND p.owner_id=? LIMIT 1""", (broker_id, owner["id"]))
    broker = get_one("SELECT * FROM users WHERE id=? AND role='broker'", (broker_id,))
    if not share or not broker:
        raise HTTPException(404, "Broker not found")
    if broker["password_hash"]:
        raise HTTPException(409, "This broker has already set their password")
    link = issue_invite(broker, owner)
    emailed = send_invite_email(broker, owner, share["address"], share["access"], link)
    result = {"ok": True, "emailed": emailed}
    if not email_configured():
        result["invite_link"] = link
    return result


@router.patch("/properties/{property_id}/brokers/{broker_id}", dependencies=[Depends(check_csrf)])
def change_access(property_id: str, broker_id: str, body: AccessRequest, owner: dict = Depends(require_agent)):
    require_property(owner, property_id, "agent")
    with db() as c:
        changed = c.execute("UPDATE property_brokers SET access=? WHERE property_id=? AND broker_id=?",
                            (body.access, property_id, broker_id)).rowcount
    if not changed:
        raise HTTPException(404, "Broker not found on this property")
    return {"ok": True}


@router.delete("/properties/{property_id}/brokers/{broker_id}", dependencies=[Depends(check_csrf)])
def remove_broker(property_id: str, broker_id: str, owner: dict = Depends(require_agent)):
    require_property(owner, property_id, "agent")
    with db() as c:
        removed = c.execute("DELETE FROM property_brokers WHERE property_id=? AND broker_id=?",
                            (property_id, broker_id)).rowcount
    if not removed:
        raise HTTPException(404, "Broker not found on this property")
    return {"ok": True}
