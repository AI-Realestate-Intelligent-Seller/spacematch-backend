"""SpaceMatch Reel Analytics API v1 - called by the Hub server with an API key.

Conventions: JSON over HTTPS, `Authorization: Bearer sk_…`, ISO 8601 UTC with `Z`,
`?limit=&cursor=` paging -> {"items": [...], "next_cursor": … | null}, counts are `null` when unknown,
errors are {"error": {"code", "message", "field"}}, POSTs that create accept `Idempotency-Key`,
every response has `X-Request-Id`; rate-limit headers on authenticated calls.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.services import refresh as rf
from app.api.apikeys import ApiError, require_scope
from app.core.settings import MANUAL_REFRESH_COOLDOWN_S
from app.db.storage import db, get_many, get_one, now, uid

router = APIRouter(prefix="/v1", tags=["Hub API v1"])
VERSION = "1.0.0"


# ----------------------------------------------------------------- helpers ----

def page_args(limit: int, cursor: str | None) -> tuple[int, int]:
    offset = 0
    if cursor:
        try:
            offset = int(json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["o"])
        except Exception:
            raise ApiError(422, "validation_failed", "Invalid cursor", "cursor")
    return limit, max(0, offset)


def page(items: list, limit: int, offset: int) -> dict:
    """`items` was fetched with limit+1 rows."""
    more = len(items) > limit
    nxt = base64.urlsafe_b64encode(json.dumps({"o": offset + limit}).encode()).decode().rstrip("=") if more else None
    return {"items": items[:limit], "next_cursor": nxt}


def parse_time(value: str | None, field: str) -> str | None:
    """Accepts ISO 8601 (with Z or offset, or a date); returns the stored '+00:00' form for comparisons."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(422, "validation_failed", "Use ISO 8601, e.g. 2026-11-18T13:00:00Z", field)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def property_object(p: dict, include: set[str] = frozenset()) -> dict:
    out = {"id": p["id"], "address": p["address"], "active": bool(p["active"]),
           "created_at": rf.iso(p["created_at"]), "updated_at": rf.iso(p["updated_at"] or p["created_at"]),
           "archived_at": rf.iso(p["archived_at"])}
    if "links" in include:
        out["links"] = links_list(p)
    if "analytics" in include:
        out["analytics"] = rf.property_analytics(p)
    return out


def links_list(p: dict) -> list[dict]:
    return [rf.link_object(link) for link in rf.current_links(p["id"])]


def get_property(key: dict, property_id: str) -> dict:
    p = get_one("SELECT * FROM properties WHERE id=? AND owner_id=?", (property_id, key["owner_id"]))
    if not p:
        raise ApiError(404, "not_found", "Property not found")
    return p


def actor(key: dict) -> str:
    return f"api_key:{key['id']}"


def link_error(exc: "rf.LinkError") -> ApiError:
    return ApiError(422, exc.code, exc.message, exc.field)


def request_fingerprint(body) -> str:
    """Stable hash of a request body (a pydantic model or None)."""
    data = body.model_dump(mode="json") if body is not None else None
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def idempotent(request: Request, key: dict, run, body=None) -> JSONResponse:
    """Replays the first response for a repeated Idempotency-Key (per API key).
    The same key with a different method, path or body is a 409 conflict."""
    idem = request.headers.get("Idempotency-Key")
    fingerprint = request_fingerprint(body)
    if idem:
        if len(idem) > 255:
            raise ApiError(422, "validation_failed", "Idempotency-Key is too long", "Idempotency-Key")
        prior = get_one("SELECT * FROM idempotency_keys WHERE key_id=? AND idem_key=?", (key["id"], idem))
        if prior:
            if (prior["method"] != request.method or prior["path"] != request.url.path
                    or (prior.get("request_hash") and prior["request_hash"] != fingerprint)):
                raise ApiError(409, "conflict", "This Idempotency-Key was already used for a different request")
            return JSONResponse(json.loads(prior["body"]), status_code=prior["status_code"],
                                headers={"Idempotent-Replayed": "true"})
    status, result = run()
    if idem:
        with db() as c:
            c.execute("INSERT INTO idempotency_keys(key_id,idem_key,method,path,status_code,body,request_hash,"
                      "created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key_id,idem_key) DO NOTHING",
                      (key["id"], idem, request.method, request.url.path, status, json.dumps(result), fingerprint, now()))
    return JSONResponse(result, status_code=status)


def queue_link_refresh(p: dict, reel_ids: list[str], key: dict, request: Request) -> dict | None:
    """New links are read right away (trigger link_change). Returns the refresh, if any."""
    if not reel_ids or not p["active"]:
        return None
    existing = rf.active_refresh(p["id"])
    if existing:
        return rf.refresh_object(existing)
    return rf.refresh_object(rf.create_refresh(p, "link_change", None, actor(key), request.state.request_id,
                                               reel_ids=reel_ids))


# ------------------------------------------------------------------ system ----

@router.get("/health")
def health():
    return {"ok": True, "version": VERSION, "time": rf.iso(now())}


@router.get("/settings/refresh-schedule")
def refresh_schedule(key: dict = Depends(require_scope("properties:read"))):
    return rf.schedule_info()


# -------------------------------------------------------------- properties ----

class CreatePropertyBody(BaseModel):
    address: str = Field(min_length=4, max_length=240)
    links: list[str] = Field(min_length=1, max_length=50,
                             description="At least one reel link (Instagram, Facebook, TikTok or YouTube). "
                                         "Several links for the same platform are allowed.")


class UpdatePropertyBody(BaseModel):
    address: str = Field(min_length=4, max_length=240)


@router.post("/properties", status_code=201)
def create_property(request: Request, body: CreatePropertyBody,
                    key: dict = Depends(require_scope("properties:write"))):
    """Create a property with its links. One bad link -> 422 and nothing is saved. New links are read right away."""
    def run():
        try:
            p, reels = rf.create_property(key["owner_id"], body.address, body.links, actor(key),
                                          property_id="prop_" + uid().replace("-", "")[:20])
        except rf.LinkError as exc:
            raise link_error(exc)
        refresh = queue_link_refresh(p, [r["id"] for r in reels], key, request)
        return 201, {**property_object(p, {"links"}), "refresh": refresh}

    return idempotent(request, key, run, body)


@router.get("/properties")
def list_properties(key: dict = Depends(require_scope("properties:read")),
                    active: bool | None = None, q: str | None = None, updated_since: str | None = None,
                    include: str = "", limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
    limit, offset = page_args(limit, cursor)
    where, args = ["owner_id=?"], [key["owner_id"]]
    if active is not None:
        where.append("active=?"); args.append(int(active))
    if q:
        where.append("lower(address) LIKE lower(?)"); args.append(f"%{q}%")
    if since := parse_time(updated_since, "updated_since"):
        where.append("COALESCE(updated_at, created_at) > ?"); args.append(since)
    rows = get_many(f"SELECT * FROM properties WHERE {' AND '.join(where)} ORDER BY created_at, rowid LIMIT ? OFFSET ?",
                    (*args, limit + 1, offset))
    inc = set(include.split(","))
    return page([property_object(p, inc) for p in rows], limit, offset)


@router.get("/properties/{property_id}")
def read_property(property_id: str, include: str = "links", key: dict = Depends(require_scope("properties:read"))):
    return property_object(get_property(key, property_id), set(include.split(",")))


@router.patch("/properties/{property_id}")
def patch_property(property_id: str, body: UpdatePropertyBody, key: dict = Depends(require_scope("properties:write"))):
    p = get_property(key, property_id)
    with db() as c:
        c.execute("UPDATE properties SET address=?, updated_at=? WHERE id=?", (body.address.strip(), now(), p["id"]))
    return property_object(get_property(key, property_id), {"links"})


@router.post("/properties/{property_id}/archive")
def archive_property(property_id: str, key: dict = Depends(require_scope("properties:write"))):
    """Sold / withdrawn: stops daily refreshes, keeps history."""
    rf.set_archived(get_property(key, property_id)["id"], True)
    return property_object(get_property(key, property_id))


@router.post("/properties/{property_id}/unarchive")
def unarchive_property(property_id: str, key: dict = Depends(require_scope("properties:write"))):
    rf.set_archived(get_property(key, property_id)["id"], False)
    return property_object(get_property(key, property_id))


@router.delete("/properties/{property_id}", status_code=204)
def delete_property(property_id: str, hard: bool = False, key: dict = Depends(require_scope("properties:write"))):
    p = get_property(key, property_id)
    reel_ids = [r["reel_id"] for r in get_many("SELECT DISTINCT reel_id FROM link_history WHERE property_id=?", (p["id"],))]
    with db() as c:
        c.execute("DELETE FROM properties WHERE id=?", (p["id"],))  # cascades links, link history, refreshes
        if hard:
            c.execute("DELETE FROM read_logs WHERE property_id=?", (p["id"],))
            for rid in reel_ids:  # reels no other property uses: delete with their snapshots
                c.execute("DELETE FROM reels WHERE id=? AND NOT EXISTS(SELECT 1 FROM link_history h WHERE h.reel_id=?)",
                          (rid, rid))
    return Response(status_code=204)


# ------------------------------------------------------------------- links ----

class LinkBody(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


class LinksBody(BaseModel):
    links: list[str] = Field(min_length=1, max_length=50)


@router.get("/properties/{property_id}/links")
def get_links(property_id: str, key: dict = Depends(require_scope("socials:read"))):
    p = get_property(key, property_id)
    return {"property_id": p["id"], "links": links_list(p)}


@router.post("/properties/{property_id}/links", status_code=201)
def add_link(property_id: str, body: LinkBody, request: Request, key: dict = Depends(require_scope("socials:write"))):
    """Attach one more link (any platform; several per platform are allowed). It is read right away."""
    p = get_property(key, property_id)
    try:
        reel, added = rf.add_link(p, body.url, actor(key))
    except rf.LinkError as exc:
        raise link_error(exc)
    link = next(link for link in rf.current_links(p["id"]) if link["id"] == reel["id"])
    return {"link": rf.link_object(link), "added": added,
            "refresh": queue_link_refresh(p, [reel["id"]] if added else [], key, request)}


@router.put("/properties/{property_id}/links")
def replace_links(property_id: str, body: LinksBody, request: Request,
                  key: dict = Depends(require_scope("socials:write"))):
    """Replace all links with this list (at least one). One bad link -> 422 and nothing is saved."""
    p = get_property(key, property_id)
    try:
        added, removed = rf.replace_links(p, body.links, actor(key))
    except rf.LinkError as exc:
        raise link_error(exc)
    return {"property_id": p["id"], "links": links_list(p), "added": added, "removed": removed,
            "refresh": queue_link_refresh(p, added, key, request)}


@router.delete("/properties/{property_id}/links/{link_id}", status_code=204)
def delete_link(property_id: str, link_id: str, key: dict = Depends(require_scope("socials:write"))):
    """Detach one link (history kept). The last link can't be removed: a property needs at least one."""
    p = get_property(key, property_id)
    try:
        removed = rf.remove_reel(p["id"], link_id, actor(key))
    except rf.LinkError as exc:
        raise ApiError(409, "conflict", exc.message)
    if not removed:
        raise ApiError(404, "not_found", "Link not found on this property")
    return Response(status_code=204)


@router.post("/links/validate")
def validate_link(body: LinkBody, key: dict = Depends(require_scope("socials:read"))):
    """Check a link without saving or reading it; the platform is detected from the URL."""
    try:
        parsed = rf.parse_link(body.url)
    except rf.LinkError as exc:
        raise link_error(exc)
    return {"valid": True, "platform": parsed["platform"], "canonical_url": parsed["url"], "embed_url": parsed["embed_url"]}


@router.get("/properties/{property_id}/links/history")
def link_history(property_id: str, platform: str | None = None, key: dict = Depends(require_scope("socials:read"))):
    p = get_property(key, property_id)
    sql, args = "SELECT * FROM link_history WHERE property_id=?", [p["id"]]
    if platform:
        sql += " AND platform=?"; args.append(platform)
    rows = get_many(sql + " ORDER BY linked_at DESC, rowid DESC", args)
    return {"items": [{"link_id": r["reel_id"], "platform": r["platform"], "url": r["url"],
                       "linked_at": rf.iso(r["linked_at"]), "linked_by": r["linked_by"],
                       "unlinked_at": rf.iso(r["unlinked_at"]), "unlinked_by": r["unlinked_by"],
                       "current": r["unlinked_at"] is None} for r in rows]}


# --------------------------------------------------------------- analytics ----

@router.get("/properties/{property_id}/analytics")
def property_analytics(property_id: str, platform: str | None = None,
                       key: dict = Depends(require_scope("socials:read"))):
    return rf.property_analytics(get_property(key, property_id), platform)


@router.get("/analytics")
def bulk_analytics(key: dict = Depends(require_scope("socials:read")), property_ids: str | None = None,
                   updated_since: str | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
    limit, offset = page_args(limit, cursor)
    where, args = ["p.owner_id=?"], [key["owner_id"]]
    if property_ids:
        ids = [x for x in property_ids.split(",") if x][:200]
        where.append(f"p.id IN ({','.join('?' for _ in ids)})"); args += ids
    if since := parse_time(updated_since, "updated_since"):
        where.append("EXISTS(SELECT 1 FROM read_logs l WHERE l.property_id=p.id AND l.at > ?)"); args.append(since)
    rows = get_many(f"SELECT p.* FROM properties p WHERE {' AND '.join(where)} ORDER BY p.created_at, p.rowid "
                    "LIMIT ? OFFSET ?", (*args, limit + 1, offset))
    return page([rf.property_analytics(p) for p in rows], limit, offset)


# ---------------------------------------------------------------- refreshes ----

class RefreshBody(BaseModel):
    platforms: list[Literal["instagram", "facebook", "tiktok", "youtube"]] | None = None
    requested_by: str | None = Field(default=None, max_length=254)


@router.post("/properties/{property_id}/refresh", status_code=202)
def refresh_property(property_id: str, request: Request, body: RefreshBody | None = None,
                     key: dict = Depends(require_scope("refresh:write"))):
    """Queues a refresh and answers `202` with it. If one is already queued/running, returns that one."""
    p = get_property(key, property_id)

    def run():
        existing = rf.active_refresh(p["id"])
        if existing:
            return 202, {"refresh": rf.refresh_object(existing)}
        if not p["active"]:
            raise ApiError(409, "conflict", "This property is archived - unarchive it to refresh")
        platforms = body.platforms if body and body.platforms else None
        if not [l for l in rf.current_links(p["id"]) if not platforms or l["platform"] in platforms]:
            raise ApiError(422, "validation_failed", "This property has no links for those platforms", "platforms")
        last = get_one("SELECT created_at FROM refreshes WHERE property_id=? AND trigger='manual' "
                       "ORDER BY created_at DESC LIMIT 1", (p["id"],))
        if last:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])).total_seconds()
            if age < MANUAL_REFRESH_COOLDOWN_S:
                wait = int(MANUAL_REFRESH_COOLDOWN_S - age) + 1
                raise ApiError(429, "rate_limited", f"This property was refreshed recently - try again in {wait} s",
                               headers={"Retry-After": str(wait)})
        r = rf.create_refresh(p, "manual", platforms,
                              (body.requested_by if body else None) or actor(key), request.state.request_id)
        return 202, {"refresh": rf.refresh_object(r)}

    return idempotent(request, key, run, body)


class BulkRefreshBody(BaseModel):
    property_ids: list[str] | None = Field(default=None, max_length=500)
    all_active: bool = False


@router.post("/refreshes", status_code=202)
def bulk_refresh(body: BulkRefreshBody, request: Request, key: dict = Depends(require_scope("refresh:write"))):
    if not body.all_active and not body.property_ids:
        raise ApiError(422, "validation_failed", "Send property_ids or all_active: true", "property_ids")

    def run():
        if body.all_active:
            props = get_many("SELECT * FROM properties WHERE owner_id=? AND active=1", (key["owner_id"],))
        else:
            props = [get_property(key, pid) for pid in dict.fromkeys(body.property_ids)]
        queued, skipped = [], []
        for p in props:
            if not p["active"]:
                skipped.append({"property_id": p["id"], "reason": "archived"})
            elif not rf.current_links(p["id"]):
                skipped.append({"property_id": p["id"], "reason": "no_links"})
            elif existing := rf.active_refresh(p["id"]):
                queued.append(rf.refresh_object(existing))
            else:
                queued.append(rf.refresh_object(rf.create_refresh(p, "bulk", None, actor(key), request.state.request_id)))
        return 202, {"refreshes": queued, "skipped": skipped}

    return idempotent(request, key, run, body)


def get_refresh(key: dict, refresh_id: str) -> dict:
    r = get_one("SELECT * FROM refreshes WHERE id=? AND owner_id=?", (refresh_id, key["owner_id"]))
    if not r:
        raise ApiError(404, "not_found", "Refresh not found")
    return r


@router.get("/refreshes/{refresh_id}")
def read_refresh(refresh_id: str, key: dict = Depends(require_scope("socials:read"))):
    return rf.refresh_object(get_refresh(key, refresh_id))


@router.post("/refreshes/{refresh_id}/cancel")
def cancel(refresh_id: str, key: dict = Depends(require_scope("refresh:write"))):
    r = get_refresh(key, refresh_id)
    if not rf.cancel_refresh(r["id"]):
        raise ApiError(409, "conflict", f"Only queued refreshes can be cancelled (this one is {r['status']})")
    return rf.refresh_object(get_refresh(key, refresh_id))


@router.get("/refreshes")
def list_refreshes(key: dict = Depends(require_scope("socials:read")), property_id: str | None = None,
                   trigger: str | None = None, status: str | None = None, from_: str | None = Query(None, alias="from"),
                   to: str | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
    limit, offset = page_args(limit, cursor)
    where, args = ["owner_id=?"], [key["owner_id"]]
    for col, val in (("property_id", property_id), ("trigger", trigger), ("status", status)):
        if val:
            where.append(f"{col}=?"); args.append(val)
    if start := parse_time(from_, "from"):
        where.append("created_at >= ?"); args.append(start)
    if end := parse_time(to, "to"):
        where.append("created_at <= ?"); args.append(end)
    rows = get_many(f"SELECT * FROM refreshes WHERE {' AND '.join(where)} ORDER BY created_at DESC, rowid DESC "
                    "LIMIT ? OFFSET ?", (*args, limit + 1, offset))
    return page([rf.refresh_object(r) for r in rows], limit, offset)


# --------------------------------------------------------- records and logs ----

@router.get("/properties/{property_id}/history")
def history(property_id: str, key: dict = Depends(require_scope("socials:read")), platform: str | None = None,
            from_: str | None = Query(None, alias="from"), to: str | None = None,
            interval: Literal["raw", "day"] = "raw", limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
    """One row per successful read of any link this property has had (newest first)."""
    p = get_property(key, property_id)
    limit, offset = page_args(limit, cursor)
    where = ["s.reel_id IN (SELECT reel_id FROM link_history WHERE property_id=?)"]
    args: list = [p["id"]]
    if platform:
        where.append("r.platform=?"); args.append(platform)
    if start := parse_time(from_, "from"):
        where.append("s.fetched_at >= ?"); args.append(start)
    if end := parse_time(to, "to"):
        where.append("s.fetched_at <= ?"); args.append(end)
    sql = (f"SELECT s.*, r.platform, r.permalink FROM metric_snapshots s JOIN reels r ON r.id=s.reel_id "
           f"WHERE {' AND '.join(where)} ORDER BY s.fetched_at DESC, s.rowid DESC")
    if interval == "raw":
        rows = get_many(sql + " LIMIT ? OFFSET ?", (*args, limit + 1, offset))
    else:  # last good read per platform per UTC day
        seen, rows = set(), []
        for r in get_many(sql, args):
            day = (r["platform"], r["fetched_at"][:10])
            if day not in seen:
                seen.add(day)
                rows.append(r)
        rows = rows[offset:offset + limit + 1]
    items = []
    for r in rows:
        m = json.loads(r["metrics_json"])
        items.append({"at": rf.iso(r["fetched_at"]), "platform": r["platform"], "url": r["permalink"],
                      **{k: m.get(k) for k in rf.COUNT_KEYS},
                      "source": rf.source_code(r["platform"], r["source_detail"]) if r["source"] == rf.PUBLIC_SOURCE else r["source"],
                      "refresh_id": r["refresh_id"], "trigger": r["trigger"]})
    return page(items, limit, offset)


@router.get("/properties/{property_id}/logs")
def logs(property_id: str, key: dict = Depends(require_scope("logs:read")), platform: str | None = None,
         status: Literal["ok", "failed"] | None = None, from_: str | None = Query(None, alias="from"),
         to: str | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
    """Every read attempt for this property, failures included (newest first)."""
    p = get_property(key, property_id)
    limit, offset = page_args(limit, cursor)
    where, args = ["property_id=?"], [p["id"]]
    for col, val in (("platform", platform), ("status", status)):
        if val:
            where.append(f"{col}=?"); args.append(val)
    if start := parse_time(from_, "from"):
        where.append("at >= ?"); args.append(start)
    if end := parse_time(to, "to"):
        where.append("at <= ?"); args.append(end)
    rows = get_many(f"SELECT * FROM read_logs WHERE {' AND '.join(where)} ORDER BY at DESC, rowid DESC LIMIT ? OFFSET ?",
                    (*args, limit + 1, offset))
    return page([{"at": rf.iso(r["at"]), "refresh_id": r["refresh_id"], "trigger": r["trigger"],
                  "platform": r["platform"], "url": r["url"], "status": r["status"],
                  "error": {"code": r["error_code"], "message": r["error_message"]} if r["status"] == "failed" else None,
                  "duration_ms": r["duration_ms"], "request_id": r["request_id"]} for r in rows], limit, offset)
