"""SpaceMatch Reels MVP v0.6 — FastAPI application.

Dashboard (/api, cookie sessions): agents sign up, create properties and invite brokers
with view or full access. Every property, social link, snapshot, export and job is scoped to the
users who can access that property. See auth.py, access.py and brokers.py.

Hub API (/v1, API keys): the Hub server manages properties and links and reads analytics.
See api_v1.py, apikeys.py and refresh.py.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.api import api_v1
# import api_v2  # Instagram profile scan (/v2) - disabled for now
from app.api import apikeys
from app.api import auth
from app.api import brokers
from app.api import recovery
from app.sources import playwright_scraper
from app.services import refresh as rf
from app.services import scans
from app.api.access import accessible_properties, require_property, require_reel
from app.api.apikeys import ApiError
from app.api.auth import check_csrf, current_user, ensure_csrf, public_user, require_agent, session_user
from app.core.settings import BASE_URL, IS_LOCAL, SESSION_SECRET
from app.db.storage import db, get_many, get_one, init_db, now, owner_reels

ROOT = Path(__file__).resolve().parent  # app/ (holds static/)
INDEX = ROOT / "static" / "index.html"


@asynccontextmanager
async def lifespan(_app):
    rf.start_background()  # refresh queue worker + daily schedule (REFRESH_WORKER=off disables)
    yield


app = FastAPI(title="SpaceMatch Reel Analytics", version="0.6.0", docs_url="/api/docs", lifespan=lifespan,
              description="Dashboard API (`/api`, cookie session) and Hub API (`/v1`, `Authorization: Bearer sk_…`).")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   session_cookie="spacematch_reels_session", max_age=14 * 24 * 3600,
                   same_site="lax", https_only=BASE_URL.startswith("https://"))
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.include_router(auth.router)
app.include_router(brokers.router)
app.include_router(recovery.router)
app.include_router(apikeys.router)
app.include_router(api_v1.router)
# app.include_router(api_v2.router)  # Instagram profile scan (/v2) - disabled for now
init_db()

# ------------------------------------------------- request ids + /v1 errors ----

REQUEST_ID_RE = re.compile(r"^[\w.:-]{1,64}$")
ERROR_CODES = {400: "validation_failed", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 422: "validation_failed", 429: "rate_limited"}


@app.middleware("http")
async def request_id(request: Request, call_next):
    incoming = request.headers.get("X-Request-Id", "")
    request.state.request_id = incoming if REQUEST_ID_RE.match(incoming) else "req_" + uuid.uuid4().hex[:20]
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


def is_v1(request: Request) -> bool:
    """Hub API paths (/v1, /v2) answer errors as {"error": {...}}."""
    return request.url.path.split("/")[1:2] in (["v1"], ["v2"])


def error_body(code: str, message: str, field: str | None = None) -> dict:
    return {"error": {"code": code, "message": message, "field": field}}


@app.exception_handler(ApiError)
async def api_error(request: Request, exc: ApiError):
    return JSONResponse(error_body(exc.code, exc.message, exc.field), status_code=exc.status, headers=exc.headers)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc):
    if not is_v1(request):
        return await http_exception_handler(request, exc)
    return JSONResponse(error_body(ERROR_CODES.get(exc.status_code, "internal"), str(exc.detail)),
                        status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not is_v1(request):
        return await request_validation_exception_handler(request, exc)
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", ())[1:]) or None
    return JSONResponse(error_body("validation_failed", first.get("msg", "Invalid request"), field), status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    if not is_v1(request):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return JSONResponse(error_body("internal", f"Unexpected error (request {request.state.request_id})"),
                        status_code=500, headers={"X-Request-Id": request.state.request_id})

CSRF = [Depends(check_csrf)]


class CreatePropertyRequest(BaseModel):
    address: str = Field(min_length=4, max_length=240)
    links: list[str] = Field(min_length=1, max_length=50)  # at least one reel link, any platform


class EditPropertyRequest(BaseModel):
    address: str = Field(min_length=4, max_length=240)


class LinkRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


class FetchPublicRequest(BaseModel):
    show_window: bool = False  # open a visible Chromium window on the server computer
    reel_ids: list[str] | None = None  # default: every link on the property


def link_problem(exc: rf.LinkError) -> HTTPException:
    """422 with a readable message; 'links[1]' -> 'Link 2: ...'."""
    field = exc.field or ""
    if field.startswith("links[") and field.endswith("]"):
        return HTTPException(422, f"Link {int(field[6:-1]) + 1}: {exc.message}")
    return HTTPException(422, exc.message)


def property_reel_list(prop: dict) -> list[dict]:
    # Social links are stored under the property's agent (owner_id), whoever added them.
    return [r for r in owner_reels(prop["owner_id"], property_id=prop["id"]) if r.get("source") != "demo"]


def property_summary(prop: dict) -> dict:
    reels = property_reel_list(prop)
    views = [r["metrics"]["views"] for r in reels if r.get("metrics") and r["metrics"].get("views") is not None]
    owner = get_one("SELECT name FROM users WHERE id=?", (prop["owner_id"],)) or {}
    return {**prop, "agent_name": owner.get("name"), "reel_count": len(reels),
            "total_views": sum(views) if views else None}


# ---------------------------------------------------------------- misc ----

@app.get("/health")
def health():
    return {"ok": True, "environment": "local-mvp" if IS_LOCAL else "server"}


@app.get("/api/me")
def me(request: Request):
    return {"user": public_user(session_user(request)), "csrf": ensure_csrf(request),
            "local_only": IS_LOCAL, "public_metrics_provider": "playwright",
            "can_show_window": playwright_scraper.can_show_window()}


# ---------------------------------------------------------- properties ----

@app.get("/api/properties")
def list_properties(user: dict = Depends(current_user)):
    return [property_summary(p) for p in accessible_properties(user)]


@app.post("/api/properties", dependencies=CSRF)
def add_property(body: CreatePropertyRequest, user: dict = Depends(require_agent)):
    """Create a property with its links (at least one). One bad link -> 422 and nothing is saved."""
    try:
        prop, _ = rf.create_property(user["id"], body.address, body.links, f"user:{user['id']}")
    except rf.LinkError as exc:
        raise link_problem(exc) from exc
    return {**prop, "access": "agent"}


@app.get("/api/properties/{property_id}")
def get_property(property_id: str, user: dict = Depends(current_user)):
    prop, access = require_property(user, property_id)
    result = {**property_summary(prop), "access": access}
    if access == "agent":
        result["brokers"] = get_many("""SELECT u.id, u.name, u.email, u.brokerage, pb.access,
                                        u.password_hash IS NOT NULL AS active
                                        FROM property_brokers pb JOIN users u ON u.id=pb.broker_id
                                        WHERE pb.property_id=? ORDER BY u.name""", (property_id,))
    return result


@app.patch("/api/properties/{property_id}", dependencies=CSRF)
def edit_property(property_id: str, body: EditPropertyRequest, user: dict = Depends(current_user)):
    require_property(user, property_id, "agent")
    with db() as c:
        c.execute("UPDATE properties SET address=?, updated_at=? WHERE id=?", (body.address.strip(), now(), property_id))
    return {"ok": True}


@app.post("/api/properties/{property_id}/archive", dependencies=CSRF)
def archive_property(property_id: str, user: dict = Depends(current_user)):
    """Sold / withdrawn: stops the daily refresh, keeps links and history."""
    require_property(user, property_id, "agent")
    rf.set_archived(property_id, True)
    return {"ok": True}


@app.post("/api/properties/{property_id}/unarchive", dependencies=CSRF)
def unarchive_property(property_id: str, user: dict = Depends(current_user)):
    require_property(user, property_id, "agent")
    rf.set_archived(property_id, False)
    return {"ok": True}


@app.delete("/api/properties/{property_id}", dependencies=CSRF)
def delete_property(property_id: str, user: dict = Depends(current_user)):
    prop, _ = require_property(user, property_id, "agent")
    with db() as c:
        c.execute("DELETE FROM properties WHERE id=?", (property_id,))  # cascades links + broker access
        c.execute("DELETE FROM reels WHERE owner_id=? AND account_id IS NULL AND "
                  "NOT EXISTS(SELECT 1 FROM property_reels pr WHERE pr.reel_id=reels.id)", (prop["owner_id"],))
    return {"ok": True}


# -------------------------------------------------- social links (reels) ----

@app.get("/api/properties/{property_id}/reels")
def property_reels(property_id: str, user: dict = Depends(current_user)):
    prop, access = require_property(user, property_id)
    return {"property": {**prop, "access": access}, "reels": property_reel_list(prop)}


@app.post("/api/properties/{property_id}/reels", dependencies=CSRF)
def attach_reel(property_id: str, body: LinkRequest, user: dict = Depends(current_user)):
    """Attach one more link (any platform; several per platform are allowed)."""
    prop, _ = require_property(user, property_id, "full")
    try:
        reel, added = rf.add_link(prop, body.url, f"user:{user['id']}")
    except rf.LinkError as exc:
        raise link_problem(exc) from exc
    return {"ok": True, "reel": reel, "added": added}


@app.delete("/api/properties/{property_id}/reels/{reel_id}", dependencies=CSRF)
def detach_reel(property_id: str, reel_id: str, user: dict = Depends(current_user)):
    require_property(user, property_id, "full")
    try:
        removed = rf.remove_reel(property_id, reel_id, f"user:{user['id']}")
    except rf.LinkError as exc:  # the last link can't be removed
        raise HTTPException(409, exc.message) from exc
    if not removed:
        raise HTTPException(404, "Reel not found")
    return {"ok": True}


@app.get("/api/reels/{reel_id}/history")
def reel_history(reel_id: str, user: dict = Depends(current_user)):
    require_reel(user, reel_id)
    return get_many("SELECT fetched_at,metrics_json,errors_json,source,source_detail FROM metric_snapshots "
                    "WHERE reel_id=? ORDER BY fetched_at DESC,rowid DESC LIMIT 100", (reel_id,))


# ------------------------------------------------------ public counters ----
# Dashboard fetches run right away with a live viewer; they are saved as refreshes (trigger "manual")
# with the same read logs and snapshots as Hub-API refreshes.

PUBLIC_SOURCE = rf.PUBLIC_SOURCE


def public_fetchable(reel):
    return not reel.get("account_id") and reel.get("source") != "demo" and reel.get("auth_kind") != "demo"


def run_dashboard_refresh(job, refresh_id):
    """Worker thread: run one refresh while streaming frames/log lines to the live viewer."""
    if not playwright_scraper.RUN_LOCK.acquire(blocking=False):
        job.say("Another browser job is running - waiting for it to finish")
        playwright_scraper.RUN_LOCK.acquire()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            rf.process_refresh(refresh_id, pw, job=job, claimed=True)
        failed = [r for r in job.results if not r["ok"]]
        job.status = "error" if failed and len(failed) == len(job.results) else "done"
    except Exception as exc:
        job.say(f"Browser error: {exc}")
        job.status = "error"
        rf.fail_refresh(refresh_id)
    finally:
        job.current = None
        job.finished_at = time.time()
        playwright_scraper.RUN_LOCK.release()


def start_dashboard_refresh(user, prop, reel_ids, show_window):
    wanted = [link["id"] for link in rf.current_links(prop["id"])
              if (not reel_ids or link["id"] in reel_ids) and public_fetchable(link)]
    if not wanted:
        raise HTTPException(422, "No social links on this property yet")
    r = rf.create_refresh(prop, "manual", None, user["email"], status="running", reel_ids=wanted if reel_ids else None)
    job = playwright_scraper.new_job(wanted, show_window, user_id=user["id"])
    job.say(f"Starting browser ({'visible window' if show_window else 'hidden'}) for {len(wanted)} link(s)")
    asyncio.get_running_loop().run_in_executor(None, run_dashboard_refresh, job, r["id"])
    return {"ok": True, "job_id": job.id, "refresh_id": r["id"], "reels": len(wanted)}


@app.post("/api/properties/{property_id}/fetch-public", dependencies=CSRF)
async def fetch_public_property(property_id: str, body: FetchPublicRequest | None = None,
                                user: dict = Depends(current_user)):
    prop, _ = require_property(user, property_id, "full")
    if not prop["active"]:
        raise HTTPException(409, "This property is archived - unarchive it to fetch")
    body = body or FetchPublicRequest()
    return start_dashboard_refresh(user, prop, body.reel_ids, body.show_window)


# ------------------------------------------------- Instagram profile scans ----

class ScanRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    max_reels: int = Field(default=scans.DEFAULT_REELS, ge=1, le=scans.MAX_REELS)
    show_window: bool = False


@app.post("/api/profile-scans", dependencies=CSRF)
def start_profile_scan(body: ScanRequest, user: dict = Depends(require_agent)):
    """Open the reel owner's Reels tab, scroll, collect every reel's JSON. Progress: /api/scrape-jobs/{job_id}."""
    try:
        scans.validate_url(body.url)
    except scans.ScanError as exc:
        raise HTTPException(422, str(exc)) from exc
    scan = scans.create_scan(user["id"], body.url, body.max_reels, user["email"])
    job = playwright_scraper.new_job([], body.show_window, user_id=user["id"])
    job.say(f"Profile scan: {body.url.strip()} (up to {body.max_reels} reels)")
    scans.start_scan(scan, job)
    return {"scan_id": scan["id"], "job_id": job.id}


def owned_scan(user: dict, scan_id: str) -> dict:
    row = scans.get_scan(user["id"], scan_id)
    if not row:
        raise HTTPException(404, "Scan not found")
    return row


@app.get("/api/profile-scans/{scan_id}")
def get_profile_scan(scan_id: str, user: dict = Depends(require_agent)):
    return scans.scan_object(owned_scan(user, scan_id))


@app.get("/api/profile-scans/{scan_id}/json")
def profile_scan_json(scan_id: str, user: dict = Depends(require_agent)):
    """The collected JSON - opened in a new browser tab from the dashboard."""
    row = owned_scan(user, scan_id)
    if row["status"] != "done":
        raise HTTPException(409, "The scan hasn't finished")
    text, filename = scans.scan_json(row)
    return Response(text, media_type="application/json",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "private, no-store"})


@app.get("/api/scrape-jobs/{job_id}")
def scrape_job_status(job_id: str, user: dict = Depends(current_user)):
    job = playwright_scraper.JOBS.get(job_id)
    if not job or job.user_id != user["id"]:
        raise HTTPException(404, "Job not found (the server may have restarted)")
    return job.public()


# --------------------------------------------------------------- export ----

def safe_csv_text(value):
    """Protect spreadsheet clients against formula injection in user-entered fields."""
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


@app.get("/api/properties/{property_id}/export.csv")
def export_property_reel_report(property_id: str, user: dict = Depends(current_user)):
    prop, _ = require_property(user, property_id)
    output = io.StringIO()
    writer = csv.writer(output)
    columns = ("property", "platform", "reel_url", "metric_source", "metric_source_detail",
               "updated_at", "views", "likes", "comments", "shares")
    writer.writerow(columns)
    for reel in property_reel_list(prop):
        metrics = reel.get("metrics") or {}
        writer.writerow([safe_csv_text(prop["address"]), reel["platform"],
                         safe_csv_text(reel.get("permalink")), reel.get("metric_source") or "unavailable",
                         safe_csv_text(reel.get("metric_source_detail")), reel.get("metrics_updated_at") or ""]
                        + [metrics.get(k) for k in columns[6:]])
    return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=property-reels-analytics.csv",
                             "Cache-Control": "private, no-store"})


# ------------------------------------------------------ frontend routes ----
# The dashboard is a single page app; these paths all serve index.html and app.js
# picks the view (and redirects to /login when needed).

SPA_PATHS = ("/", "/login", "/signup", "/forgot-password", "/recover", "/reset-password/{token}", "/invite/{token}", "/properties", "/properties/{property_id}", "/brokers",
             "/api-keys", "/account")
STATIC_ASSETS = ("app.js", "style.css")


def asset_version(name: str) -> str:
    return str((ROOT / "static" / name).stat().st_mtime_ns)


def spa_index():
    """index.html with versioned asset URLs, so browsers never pair new HTML with a stale cached app.js."""
    html = INDEX.read_text(encoding="utf-8")
    for name in STATIC_ASSETS:
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={asset_version(name)}"')
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always revalidate (cheap 304 via ETag)
    return response


for _path in SPA_PATHS:
    app.add_api_route(_path, spa_index, methods=["GET"], include_in_schema=False)
