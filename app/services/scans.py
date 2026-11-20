"""Instagram profile scans: open the reel owner's Reels tab, scroll, and collect every reel's public
counters as JSON. Used by the dashboard (/api/profile-scans) and the Hub API (/v2/profile-scans).

A scan runs in a background thread (one browser at a time, shared with refreshes) and its JSON is saved,
so the result can be opened later.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from app.sources import playwright_scraper as ps
from app.db.storage import db, get_one, now, uid
from app.services.urls import InvalidReelURL

DEFAULT_REELS = 30
MAX_REELS = 300
REELS_PER_SCROLL = 12  # browser fallback: roughly how many reels one scroll loads
STALE_AFTER = timedelta(minutes=20)  # a scan still "running" after this was interrupted (e.g. a restart)


class ScanError(ValueError):
    pass


def validate_url(url: str) -> None:
    try:
        ps.parse_instagram_target(url)
    except (InvalidReelURL, ValueError) as exc:
        raise ScanError(str(exc).strip()) from exc


def create_scan(owner_id: str, url: str, max_reels: int, requested_by: str | None) -> dict:
    row = {"id": "scan_" + uid().replace("-", "")[:20], "owner_id": owner_id, "url": url.strip(),
           "max_reels": max_reels, "max_scrolls": max(1, math.ceil(max_reels / REELS_PER_SCROLL)),
           "status": "running", "requested_by": requested_by, "created_at": now()}
    with db() as c:
        c.execute("INSERT INTO profile_scans(id,owner_id,url,max_reels,max_scrolls,status,requested_by,created_at) "
                  "VALUES(:id,:owner_id,:url,:max_reels,:max_scrolls,:status,:requested_by,:created_at)", row)
    return get_one("SELECT * FROM profile_scans WHERE id=?", (row["id"],))


def get_scan(owner_id: str, scan_id: str) -> dict | None:
    return get_one("SELECT * FROM profile_scans WHERE id=? AND owner_id=?", (scan_id, owner_id))


def start_scan(scan: dict, job=None) -> None:
    threading.Thread(target=run_scan, args=(scan["id"], job), name=f"scan-{scan['id']}", daemon=True).start()


def run_scan(scan_id: str, job=None) -> None:
    """Thread body: run the browser scan, save the JSON (or the error)."""
    if not ps.RUN_LOCK.acquire(blocking=False):
        if job:
            job.say("Another browser job is running - waiting for it to finish")
        ps.RUN_LOCK.acquire()
    try:
        scan = get_one("SELECT * FROM profile_scans WHERE id=?", (scan_id,))
        data = None
        from app.sources import apify_source  # Apify when configured; the local browser is the fallback
        if apify_source.enabled():
            try:
                data = apify_source.instagram_profile(scan["url"], job, max_reels=scan["max_reels"] or DEFAULT_REELS)
            except apify_source.ApifyError as exc:
                if job:
                    job.say(f"Apify unavailable ({exc}) - falling back to the browser")
        if data is None:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                data = ps.scan_instagram_profile(scan["url"], job, pw=pw, max_scrolls=scan["max_scrolls"])
        data = {"scan_id": scan_id, **data}
        with db() as c:
            c.execute("UPDATE profile_scans SET status='done', username=?, reel_count=?, result_json=?, finished_at=? "
                      "WHERE id=?", (data.get("username"), data.get("reel_count"), json.dumps(data), now(), scan_id))
        if job:
            job.status = "done"
    except Exception as exc:
        first = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
        message = first[:300] or type(exc).__name__
        with db() as c:
            c.execute("UPDATE profile_scans SET status='failed', error=?, finished_at=? WHERE id=?",
                      (message, now(), scan_id))
        if job:
            job.say(f"Failed: {message}")
            job.status = "error"
    finally:
        if job:
            job.current = None
            job.finished_at = time.time()
        ps.RUN_LOCK.release()


def _iso(ts: str | None) -> str | None:
    return ts.replace("+00:00", "Z") if ts else None


def scan_object(row: dict, include_data: bool = True) -> dict:
    status, error = row["status"], row["error"]
    if status == "running" and datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) > STALE_AFTER:
        status, error = "failed", "The scan was interrupted (server restart or timeout) - start a new one"
    out = {"id": row["id"], "status": status, "url": row["url"], "username": row["username"],
           "max_reels": row.get("max_reels"), "reel_count": row["reel_count"], "requested_by": row["requested_by"],
           "created_at": _iso(row["created_at"]), "finished_at": _iso(row["finished_at"]), "error": error}
    if include_data:
        out["data"] = json.loads(row["result_json"]) if row["result_json"] else None
    return out


def scan_json(row: dict) -> tuple[str, str]:
    """(pretty JSON text, file name) for a finished scan."""
    data = json.loads(row["result_json"])
    return json.dumps(data, indent=2, ensure_ascii=False), f"instagram-{row['username'] or 'profile'}-{row['id']}.json"
