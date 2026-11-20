"""Who can see / change a property and its social links (reels).

Access levels, strongest first:
  agent - the agent who owns the property: everything, incl. editing/deleting it and managing brokers.
  full  - broker with full access: add/remove social links, fetch counters, export.
  view  - broker with view access: see social links and metrics, export CSV.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.db.storage import get_many, get_one

RANK = {"view": 1, "full": 2, "agent": 3}


def property_access(user: dict, prop: dict) -> str | None:
    if prop["owner_id"] == user["id"]:
        return "agent"
    if user["role"] == "broker":
        row = get_one("SELECT access FROM property_brokers WHERE property_id=? AND broker_id=?",
                      (prop["id"], user["id"]))
        return row["access"] if row else None
    return None


def require_property(user: dict, property_id: str, needed: str = "view") -> tuple[dict, str]:
    """Returns (property, access). 404 when the user can't see it, 403 when access is too low."""
    prop = get_one("SELECT * FROM properties WHERE id=?", (property_id,))
    access = property_access(user, prop) if prop else None
    if not access:
        raise HTTPException(404, "Property not found")
    if RANK[access] < RANK[needed]:
        raise HTTPException(403, "You have view-only access to this property"
                            if access == "view" else "Only the property's agent can do this")
    return prop, access


def accessible_properties(user: dict) -> list[dict]:
    if user["role"] == "agent":
        rows = get_many("SELECT p.*, 'agent' AS access FROM properties p WHERE p.owner_id=? "
                        "ORDER BY p.created_at DESC", (user["id"],))
    else:
        rows = get_many("SELECT p.*, pb.access AS access FROM properties p "
                        "JOIN property_brokers pb ON pb.property_id=p.id "
                        "WHERE pb.broker_id=? ORDER BY p.created_at DESC", (user["id"],))
    return rows


def require_reel(user: dict, reel_id: str, needed: str = "view") -> tuple[dict, str]:
    """A reel is reachable through any property it is linked to; the strongest access wins."""
    reel = get_one("SELECT * FROM reels WHERE id=?", (reel_id,))
    best = None
    if reel:
        for prop in get_many("SELECT p.* FROM properties p JOIN property_reels pr ON pr.property_id=p.id "
                             "WHERE pr.reel_id=?", (reel_id,)):
            access = property_access(user, prop)
            if access and (best is None or RANK[access] > RANK[best]):
                best = access
    if not best:
        raise HTTPException(404, "Reel not found")
    if RANK[best] < RANK[needed]:
        raise HTTPException(403, "You have view-only access to this reel")
    return reel, best
