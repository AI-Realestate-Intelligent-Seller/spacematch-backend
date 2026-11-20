"""Social links, analytics, refreshes and read logs - shared by the dashboard (/api) and the Hub API (/v1).

- A property has one or more links (several may be for the same platform). Removed links stay in
  `link_history`, and their snapshots stay in `metric_snapshots`.
- Every read attempt (ok or failed) is written to `read_logs`; every good read is a snapshot.
- Refreshes are persisted (`refreshes`) and processed one at a time by a background worker.
  A scheduler queues a `daily` refresh for every active property with links.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.sources import playwright_scraper
from app.core.settings import (DAILY_REFRESH_ENABLED, DAILY_REFRESH_TIME, DAILY_REFRESH_TZ, REFRESH_WORKER)
from app.db.storage import db, get_many, get_one, known_metrics, now, save_reel, save_snapshot, uid
from app.services.urls import InvalidReelURL, parse_reel_url

log = logging.getLogger("spacematch.refresh")

PLATFORMS = ("instagram", "facebook", "tiktok", "youtube")
PUBLIC_SOURCE = "playwright_public"
SOURCE_CODES = {"instagram": "reels_tab", "facebook": "watch_page", "youtube": "watch_page", "tiktok": "video_page"}
COUNT_KEYS = ("views", "likes", "comments", "shares")
READ_DELAY_S = float(os.getenv("READ_DELAY_S", "3"))  # pause between reads in background runs


def source_code(platform: str, detail: str | None) -> str | None:
    """Short label for where a number came from."""
    if detail and "via Apify" in detail:
        return "apify"
    if detail and detail.startswith("YouTube Data API"):
        return "youtube_api"
    return SOURCE_CODES.get(platform)


def iso(ts: str | None) -> str | None:
    """Stored timestamps are '...+00:00'; the API speaks '...Z'."""
    return ts.replace("+00:00", "Z") if ts else None


# ------------------------------------------------------------------ links ----
# A property has one or more links; several links may be for the same platform.
# Each link is a reel row (its id is the link id) attached through property_reels.

class LinkError(ValueError):
    def __init__(self, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def parse_link(url, field: str = "url") -> dict:
    """Validate one link; the platform is detected from the URL."""
    if not isinstance(url, str) or not url.strip():
        raise LinkError("invalid_link", "Enter a link", field)
    try:
        return parse_reel_url(url)
    except (InvalidReelURL, ValueError) as exc:
        raise LinkError("invalid_link", str(exc).strip(), field) from exc


def parse_links(urls, field: str = "links") -> list[dict]:
    """Validate a list of links, all or nothing. At least one is required; blanks and duplicates are dropped."""
    if not isinstance(urls, list) or not any(isinstance(u, str) and u.strip() for u in urls):
        raise LinkError("validation_failed", "Add at least one link (Instagram, Facebook, TikTok or YouTube)", field)
    out, seen = [], set()
    for i, url in enumerate(urls):
        if isinstance(url, str) and not url.strip():
            continue
        parsed = parse_link(url, f"{field}[{i}]")
        if (parsed["platform"], parsed["key"]) not in seen:
            seen.add((parsed["platform"], parsed["key"]))
            out.append({**parsed, "input": url.strip()})
    return out


def current_links(property_id: str) -> list[dict]:
    """The property's links (reel rows + linked_url, linked_at, linked_by), oldest first."""
    return get_many("""SELECT r.*, pr.url AS linked_url, pr.linked_at, pr.linked_by
                       FROM property_reels pr JOIN reels r ON r.id=pr.reel_id
                       WHERE pr.property_id=? ORDER BY pr.linked_at, r.rowid""", (property_id,))


def _touch(c, property_id):
    c.execute("UPDATE properties SET updated_at=? WHERE id=?", (now(), property_id))


def _unlink(c, property_id: str, reel_id: str, actor: str | None):
    c.execute("DELETE FROM property_reels WHERE property_id=? AND reel_id=?", (property_id, reel_id))
    c.execute("UPDATE link_history SET unlinked_at=?, unlinked_by=? WHERE property_id=? AND reel_id=? "
              "AND unlinked_at IS NULL", (now(), actor, property_id, reel_id))


def _attach(c, prop: dict, parsed: dict, actor: str | None) -> tuple[dict, bool]:
    reel = save_reel(prop["owner_id"], parsed["platform"], parsed["key"], permalink=parsed["url"],
                     embed_url=parsed["embed_url"], source="url", conn=c)
    if c.execute("SELECT 1 FROM property_reels WHERE property_id=? AND reel_id=?", (prop["id"], reel["id"])).fetchone():
        return reel, False
    url = parsed.get("input") or parsed["url"]
    c.execute("INSERT INTO property_reels(property_id,reel_id,linked_at,url,linked_by) VALUES(?,?,?,?,?)",
              (prop["id"], reel["id"], now(), url, actor))
    c.execute("INSERT INTO link_history(id,property_id,platform,url,reel_id,linked_at,linked_by) VALUES(?,?,?,?,?,?,?)",
              (uid(), prop["id"], parsed["platform"], url, reel["id"], now(), actor))
    _touch(c, prop["id"])
    return reel, True


def create_property(owner_id: str, address: str, urls: list, actor: str | None = None,
                    property_id: str | None = None) -> tuple[dict, list[dict]]:
    """Create a property with its links in one transaction. Raises LinkError (nothing saved) on a bad link."""
    parsed = parse_links(urls)
    prop = {"id": property_id or uid(), "owner_id": owner_id, "address": address.strip(),
            "created_at": now(), "updated_at": now()}
    with db() as c:
        c.execute("INSERT INTO properties(id,owner_id,address,created_at,updated_at) "
                  "VALUES(:id,:owner_id,:address,:created_at,:updated_at)", prop)
        reels = [_attach(c, prop, p, actor)[0] for p in parsed]
    return get_one("SELECT * FROM properties WHERE id=?", (prop["id"],)), reels


def add_link(prop: dict, url: str, actor: str | None = None) -> tuple[dict, bool]:
    """Attach one more link. Returns (reel, added); added is False if it was already attached."""
    parsed = parse_link(url)
    with db() as c:
        return _attach(c, prop, {**parsed, "input": url.strip()}, actor)


def replace_links(prop: dict, urls: list, actor: str | None = None) -> tuple[list[str], list[str]]:
    """Make the property's links exactly `urls` (at least one). Returns (added ids, removed ids)."""
    parsed = parse_links(urls)
    before = {link["id"] for link in current_links(prop["id"])}
    with db() as c:
        keep = {}
        for p in parsed:
            reel, _ = _attach(c, prop, p, actor)
            keep[reel["id"]] = True
        removed = [rid for rid in before if rid not in keep]
        for rid in removed:
            _unlink(c, prop["id"], rid, actor)
        if removed:
            _touch(c, prop["id"])
    return [rid for rid in keep if rid not in before], removed


def remove_reel(property_id: str, reel_id: str, actor: str | None = None) -> bool:
    """Detach a link (its history is kept). The last link can't be removed: a property needs at least one."""
    with db() as c:
        if not c.execute("SELECT 1 FROM property_reels WHERE property_id=? AND reel_id=?", (property_id, reel_id)).fetchone():
            return False
        if c.execute("SELECT count(*) AS n FROM property_reels WHERE property_id=?", (property_id,)).fetchone()["n"] <= 1:
            raise LinkError("validation_failed", "A property needs at least one link - add another link before removing this one")
        _unlink(c, property_id, reel_id, actor)
        _touch(c, property_id)
    return True


def set_archived(property_id: str, archived: bool):
    """Archive (sold / withdrawn: no daily refresh, queued refreshes cancelled, history kept) or unarchive."""
    with db() as c:
        if archived:
            c.execute("UPDATE properties SET active=0, archived_at=COALESCE(archived_at, ?), updated_at=? WHERE id=?",
                      (now(), now(), property_id))
            c.execute("UPDATE refreshes SET status='cancelled', finished_at=? WHERE property_id=? AND status='queued'",
                      (now(), property_id))
        else:
            c.execute("UPDATE properties SET active=1, archived_at=NULL, updated_at=? WHERE id=?", (now(), property_id))


def last_attempt(reel_id: str) -> dict | None:
    return get_one("SELECT * FROM read_logs WHERE reel_id=? ORDER BY at DESC, rowid DESC LIMIT 1", (reel_id,))


def link_status(reel_id: str) -> str:
    attempt = last_attempt(reel_id)
    if not attempt:
        return "ok" if get_one("SELECT 1 FROM metric_snapshots WHERE reel_id=? LIMIT 1", (reel_id,)) else "pending"
    if attempt["status"] == "ok":
        return "ok"
    # invalid: the link is broken/private; blocked: the platform refused the read;
    # error: our side failed (e.g. browser missing) - not the link's or the platform's fault.
    return {"invalid_link": "invalid", "upstream_blocked": "blocked"}.get(attempt["error_code"], "error")


def link_object(reel: dict) -> dict:
    return {"id": reel["id"], "platform": reel["platform"], "url": reel.get("linked_url") or reel["permalink"],
            "canonical_url": reel["permalink"], "embed_url": reel.get("embed_url"),
            "linked_at": iso(reel.get("linked_at")), "linked_by": reel.get("linked_by"),
            "status": link_status(reel["id"])}


# -------------------------------------------------------------- analytics ----

def reel_analytics(reel: dict) -> dict:
    snap = get_one("SELECT * FROM metric_snapshots WHERE reel_id=? ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
                   (reel["id"],))
    attempt = last_attempt(reel["id"])
    metrics = known_metrics(reel["id"]) if snap else {}  # a partial read doesn't hide earlier counts
    error = None
    if attempt and attempt["status"] == "failed":
        error = {"code": attempt["error_code"], "message": attempt["error_message"]}
    return {"link_id": reel["id"], "platform": reel["platform"], "url": reel.get("linked_url") or reel["permalink"],
            "canonical_url": reel["permalink"], "embed_url": reel.get("embed_url"),
            **{k: metrics.get(k) for k in COUNT_KEYS},
            "checked_at": iso(attempt["at"] if attempt else (snap["fetched_at"] if snap else None)),
            "ok_at": iso(snap["fetched_at"]) if snap else None,
            "source": source_code(reel["platform"], snap["source_detail"]) if snap else None,
            "source_detail": snap["source_detail"] if snap else None,
            "error": error}


def property_analytics(prop: dict, platform: str | None = None) -> dict:
    links = current_links(prop["id"])
    latest = get_one("SELECT * FROM refreshes WHERE property_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                     (prop["id"],))
    daily = next_daily_run() if DAILY_REFRESH_ENABLED and prop.get("active", 1) and links else None
    return {"property_id": prop["id"],
            "last_refresh": refresh_object(latest, brief=True) if latest else None,
            "next_daily_refresh_at": iso(daily.isoformat(timespec="seconds")) if daily else None,
            "reels": [reel_analytics(r) for r in links if not platform or r["platform"] == platform]}


# -------------------------------------------------------------- refreshes ----

def active_refresh(property_id: str) -> dict | None:
    return get_one("SELECT * FROM refreshes WHERE property_id=? AND status IN ('queued','running') "
                   "ORDER BY created_at LIMIT 1", (property_id,))


def create_refresh(prop: dict, trigger: str, platforms=None, requested_by=None, request_id=None,
                   status: str = "queued", reel_ids=None) -> dict:
    """Queue (or, with status='running', claim) a refresh of the property's links.
    `platforms` / `reel_ids` narrow it to some links; by default every link is read."""
    r = {"id": "rf_" + uid().replace("-", "")[:20], "property_id": prop["id"], "owner_id": prop["owner_id"],
         "trigger": trigger, "status": status, "platforms_json": json.dumps(list(platforms)) if platforms else None,
         "reel_ids_json": json.dumps(list(reel_ids)) if reel_ids else None,
         "requested_by": requested_by, "request_id": request_id, "created_at": now(),
         "started_at": now() if status == "running" else None}
    with db() as c:
        c.execute("INSERT INTO refreshes(id,property_id,owner_id,trigger,status,platforms_json,reel_ids_json,"
                  "requested_by,request_id,created_at,started_at) VALUES(:id,:property_id,:owner_id,:trigger,"
                  ":status,:platforms_json,:reel_ids_json,:requested_by,:request_id,:created_at,:started_at)", r)
    if status == "queued":
        wake()
    return get_one("SELECT * FROM refreshes WHERE id=?", (r["id"],))


def refresh_object(r: dict, brief: bool = False) -> dict:
    out = {"id": r["id"], "property_id": r["property_id"], "trigger": r["trigger"], "status": r["status"],
           "requested_by": r["requested_by"], "created_at": iso(r["created_at"]),
           "started_at": iso(r["started_at"]), "finished_at": iso(r["finished_at"])}
    if brief:
        return out
    attempts = {a["reel_id"]: a for a in get_many("SELECT * FROM read_logs WHERE refresh_id=? ORDER BY at, rowid",
                                                  (r["id"],))}
    active = r["status"] in ("queued", "running")
    rows = {link["id"]: link for link in (refresh_targets(r) if active else [])}
    links = []
    for rid in list(rows) + [x for x in attempts if x not in rows]:
        a, link = attempts.get(rid), rows.get(rid) or {}
        links.append({"link_id": rid, "platform": (a or link)["platform"],
                      "url": a["url"] if a else link.get("permalink"),
                      "status": a["status"] if a else "pending",
                      "error": {"code": a["error_code"], "message": a["error_message"]} if a and a["status"] == "failed" else None,
                      "checked_at": iso(a["at"]) if a else None,
                      "duration_ms": a["duration_ms"] if a else None})
    return {**out, "links": links}


def refresh_targets(r: dict) -> list[dict]:
    """The links a refresh reads: the property's current links, narrowed by platforms / reel ids."""
    platforms = json.loads(r["platforms_json"]) if r["platforms_json"] else None
    reel_ids = json.loads(r["reel_ids_json"]) if r.get("reel_ids_json") else None
    return [link for link in current_links(r["property_id"])
            if (not platforms or link["platform"] in platforms) and (not reel_ids or link["id"] in reel_ids)
            and link.get("permalink") and link.get("source") != "demo"]  # skip old sample rows


def _finish(refresh_id: str, status: str):
    with db() as c:
        c.execute("UPDATE refreshes SET status=?, finished_at=? WHERE id=? AND status IN ('queued','running')",
                  (status, now(), refresh_id))


def fail_refresh(refresh_id: str):
    _finish(refresh_id, "failed")


def cancel_refresh(refresh_id: str) -> bool:
    with db() as c:
        return c.execute("UPDATE refreshes SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                         (now(), refresh_id)).rowcount == 1


INVALID_HINT = re.compile(r"private|removed|not exist|isn't available|unavailable|no video data|could not find", re.I)


def error_message(exc: Exception) -> str:
    """First line only (Playwright adds multi-line install banners), max 300 chars."""
    first = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
    return first[:300] or type(exc).__name__


def classify_error(exc: Exception) -> str:
    if isinstance(exc, playwright_scraper.ScrapeError):
        return "invalid_link" if INVALID_HINT.search(str(exc)) else "upstream_blocked"
    if "Timeout" in type(exc).__name__ or "net::" in str(exc):
        return "upstream_blocked"
    return "internal"


def _log_attempt(reel, status, *, refresh=None, property_id=None, error_code=None, error_message=None, duration_ms=None):
    with db() as c:
        c.execute("INSERT INTO read_logs(id,at,refresh_id,trigger,property_id,reel_id,platform,url,status,"
                  "error_code,error_message,duration_ms,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uid(), now(), refresh and refresh["id"], refresh and refresh["trigger"], property_id,
                   reel["id"], reel["platform"], reel["permalink"], status, error_code, error_message,
                   duration_ms, refresh and refresh.get("request_id")))


def read_reel(reel: dict, pw, job=None, refresh: dict | None = None, property_id: str | None = None) -> dict:
    """Read one link's public counters. Logs the attempt; saves a snapshot on success."""
    started = time.monotonic()
    if job:
        job.current = reel["permalink"]
        job.say(f"--- {reel['platform'].title()}: {reel['permalink']}")
    try:
        metrics = playwright_scraper.scrape(reel["permalink"], job, pw=pw)
        detail = metrics.pop("detail", None)
        embed = metrics.pop("embed_url", None)
        if embed and not reel.get("embed_url"):  # share links: preview once the video is known
            with db() as c:
                c.execute("UPDATE reels SET embed_url=? WHERE id=?", (embed, reel["id"]))
        if all(metrics.get(k) is None for k in COUNT_KEYS):
            raise playwright_scraper.ScrapeError("No public counters found for this reel")
        save_snapshot(reel["id"], metrics, {}, source=PUBLIC_SOURCE, source_detail=detail,
                      refresh_id=refresh and refresh["id"], trigger=refresh and refresh["trigger"])
        _log_attempt(reel, "ok", refresh=refresh, property_id=property_id,
                     duration_ms=int((time.monotonic() - started) * 1000))
        if job:
            job.say(f"Saved: views={metrics.get('views')}, likes={metrics.get('likes')}, "
                    f"comments={metrics.get('comments')}, shares={metrics.get('shares')}")
        return {"reel_id": reel["id"], "ok": True, "metrics": {k: metrics.get(k) for k in COUNT_KEYS}}
    except Exception as exc:  # keep going with the next link
        msg = error_message(exc)
        _log_attempt(reel, "failed", refresh=refresh, property_id=property_id, error_code=classify_error(exc),
                     error_message=msg, duration_ms=int((time.monotonic() - started) * 1000))
        if job:
            job.say(f"Failed: {msg}")
        return {"reel_id": reel["id"], "ok": False, "error": msg}


def process_refresh(refresh_id: str, pw, job=None, claimed: bool = False) -> list[dict] | None:
    """Run one refresh. Claims it (queued -> running) unless the caller already did."""
    if not claimed:
        with db() as c:
            if c.execute("UPDATE refreshes SET status='running', started_at=? WHERE id=? AND status='queued'",
                         (now(), refresh_id)).rowcount != 1:
                return None
    r = get_one("SELECT * FROM refreshes WHERE id=?", (refresh_id,))
    targets = refresh_targets(r)
    results = []
    for i, reel in enumerate(targets):
        if i and not job and READ_DELAY_S > 0:
            time.sleep(READ_DELAY_S)  # be gentle with the platforms
        results.append(read_reel(reel, pw, job, refresh=r, property_id=r["property_id"]))
    ok = sum(1 for x in results if x["ok"])
    _finish(refresh_id, "done" if ok == len(results) else ("failed" if ok == 0 else "partial"))
    if job:
        job.results.extend(results)
    return results


def run_queue() -> int:
    """Process queued refreshes, oldest first, until none are left. Returns how many ran."""
    if not get_one("SELECT 1 FROM refreshes WHERE status='queued' LIMIT 1"):
        return 0
    from playwright.sync_api import sync_playwright
    done = 0
    with playwright_scraper.RUN_LOCK, sync_playwright() as pw:
        while nxt := get_one("SELECT id FROM refreshes WHERE status='queued' ORDER BY created_at, rowid LIMIT 1"):
            try:
                if process_refresh(nxt["id"], pw) is not None:
                    done += 1
            except Exception:
                log.exception("Refresh %s crashed", nxt["id"])
                fail_refresh(nxt["id"])
    return done


# ---------------------------------------------------------- daily schedule ----

def _schedule_tz() -> ZoneInfo:
    return ZoneInfo(DAILY_REFRESH_TZ)


def _scheduled_on(day) -> datetime:
    hh, mm = (int(x) for x in DAILY_REFRESH_TIME.split(":"))
    return datetime.combine(day, dtime(hh, mm), tzinfo=_schedule_tz())


def next_daily_run(after: datetime | None = None) -> datetime:
    """Next daily run in UTC. Wall-clock time in the schedule time zone, so it follows daylight saving."""
    local = (after or datetime.now(timezone.utc)).astimezone(_schedule_tz())
    run = _scheduled_on(local.date())
    if run <= local:
        run = _scheduled_on(local.date() + timedelta(days=1))
    return run.astimezone(timezone.utc)


def schedule_info() -> dict:
    nxt = next_daily_run() if DAILY_REFRESH_ENABLED else None
    return {"enabled": DAILY_REFRESH_ENABLED, "time": DAILY_REFRESH_TIME, "timezone": DAILY_REFRESH_TZ,
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "next_run_at": iso(nxt.isoformat(timespec="seconds")) if nxt else None}


def enqueue_daily(day_key: str | None = None, force: bool = False) -> int:
    """Queue a daily refresh for every active property with at least one link (once per local day)."""
    day_key = day_key or datetime.now(timezone.utc).astimezone(_schedule_tz()).date().isoformat()
    state = get_one("SELECT v FROM app_state WHERE k='daily_last_run'")
    if state and state["v"] == day_key and not force:
        return 0
    with db() as c:
        c.execute("INSERT INTO app_state(k,v) VALUES('daily_last_run',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (day_key,))
    props = get_many("SELECT p.* FROM properties p WHERE p.active=1 AND EXISTS("
                     "SELECT 1 FROM property_reels pr WHERE pr.property_id=p.id)")
    queued = 0
    for p in props:
        if not active_refresh(p["id"]):
            create_refresh(p, "daily")
            queued += 1
    log.info("Daily refresh %s: queued %d properties", day_key, queued)
    return queued


# ------------------------------------------------------- background threads ----

_wake = threading.Event()
_started = False


def wake():
    _wake.set()


def _worker_loop():
    while True:
        _wake.wait(timeout=10)
        _wake.clear()
        try:
            run_queue()
        except Exception:
            log.exception("Refresh worker error")
            time.sleep(30)


def _scheduler_loop():
    while True:
        try:
            local = datetime.now(timezone.utc).astimezone(_schedule_tz())
            if local >= _scheduled_on(local.date()):  # today's run is due (or was missed while stopped)
                enqueue_daily(local.date().isoformat())
            wait = (next_daily_run() - datetime.now(timezone.utc)).total_seconds()
            time.sleep(max(1.0, min(wait, 300)))
        except Exception:
            log.exception("Daily scheduler error")
            time.sleep(60)


def start_background():
    global _started
    if not REFRESH_WORKER:
        print("Refresh worker + daily schedule: OFF on this server (set REFRESH_WORKER=on to run them here)", flush=True)
        return
    if _started:
        return
    print(f"Refresh worker: ON; daily schedule: {'ON' if DAILY_REFRESH_ENABLED else 'OFF'}", flush=True)
    _started = True
    threading.Thread(target=_worker_loop, name="refresh-worker", daemon=True).start()
    if DAILY_REFRESH_ENABLED:
        threading.Thread(target=_scheduler_loop, name="daily-scheduler", daemon=True).start()
    wake()  # pick up anything queued before a restart
