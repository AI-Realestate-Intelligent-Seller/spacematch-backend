"""Instagram data through Apify's Instagram Reel Scraper.

The scraper runs on Apify's servers and proxies, so it isn't blocked the way a cloud-server browser is.
It is used automatically when APIFY_TOKEN is set. ApifyError (bad token, no credit, timeout) lets callers
fall back to the local browser; ScrapeError means Apify answered but has no data for that reel.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.sources import playwright_scraper as ps
from app.core.settings import APIFY_REEL_ACTOR, APIFY_TIMEOUT_S, APIFY_TOKEN
from app.services.urls import parse_reel_url

API = "https://api.apify.com/v2"


class ApifyError(Exception):
    """Apify itself failed - the caller may fall back to the browser."""


def enabled() -> bool:
    return bool(APIFY_TOKEN)


def run_reel_scraper(targets: list[str], limit: int) -> list[dict]:
    """Run the actor synchronously and return its dataset items."""
    try:
        r = httpx.post(f"{API}/acts/{APIFY_REEL_ACTOR}/run-sync-get-dataset-items",
                       params={"timeout": APIFY_TIMEOUT_S}, headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
                       json={"username": targets, "resultsLimit": limit}, timeout=APIFY_TIMEOUT_S + 30)
    except httpx.HTTPError as exc:
        raise ApifyError(f"Apify request failed ({type(exc).__name__})") from exc
    if r.status_code == 401:
        raise ApifyError("Apify token is invalid")
    if r.status_code == 402:
        raise ApifyError("Apify account is out of credit")
    if r.status_code >= 300:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = r.text[:200]
        raise ApifyError(f"Apify error {r.status_code}: {detail}".strip(": "))
    items = r.json()
    if not isinstance(items, list):
        raise ApifyError("Apify returned an unexpected response")
    return items


def _count(value):
    return value if isinstance(value, int) and value >= 0 else None  # Instagram hides some counts (-1)


def _iso(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return None


def reel_row(item: dict) -> dict:
    code = item.get("shortCode")
    plays = item.get("videoPlayCount") if item.get("videoPlayCount") is not None else item.get("videoViewCount")
    return {"shortcode": code, "url": f"https://www.instagram.com/reel/{code}/", "play_count": _count(plays),
            "like_count": _count(item.get("likesCount")), "comment_count": _count(item.get("commentsCount")),
            "taken_at": _iso(item.get("timestamp")), "owner_username": item.get("ownerUsername")}


def _no_data(items: list[dict], what: str) -> ps.ScrapeError:
    err = next((i for i in items if i.get("error")), {})
    reason = err.get("errorDescription") or err.get("error") or "empty result"
    return ps.ScrapeError(f"Apify found no data for {what} ({reason}) - it may be private or removed")


def _say(job, msg):
    if job:
        job.say(msg)


def instagram_reel(url: str, job=None) -> dict:
    """One reel's counters - same result shape as playwright_scraper.scrape()."""
    code = parse_reel_url(url)["key"]
    _say(job, f"Reading Instagram reel {code} via Apify")
    items = run_reel_scraper([f"https://www.instagram.com/reel/{code}/"], 1)
    item = next((i for i in items if i.get("shortCode") == code and not i.get("error")), None)
    if not item:
        raise _no_data(items, "this reel")
    row = reel_row(item)
    ps.log_reel(code, row["play_count"], row["like_count"], row["comment_count"])
    _say(job, f"Apify: plays={row['play_count']}, likes={row['like_count']}, comments={row['comment_count']}")
    return {"views": row["play_count"], "likes": row["like_count"], "comments": row["comment_count"], "shares": None,
            "detail": f"Instagram via Apify Reel Scraper (@{row['owner_username']})"}


def instagram_profile(url: str, job=None, max_reels: int = 30) -> dict:
    """Every recent reel of the account behind a reel or profile link - same shape as the browser scan."""
    username, code = ps.parse_instagram_target(url)
    target_item = None
    if code:  # the pasted reel: its own numbers, and who posted it
        _say(job, f"Looking up reel {code} via Apify")
        items = run_reel_scraper([f"https://www.instagram.com/reel/{code}/"], 1)
        target_item = next((i for i in items if i.get("shortCode") == code and not i.get("error")), None)
        if not username:
            if not target_item:
                raise _no_data(items, "this reel")
            username = target_item.get("ownerUsername")
    _say(job, f"Collecting up to {max_reels} reels from @{username} via Apify")
    items = run_reel_scraper([username], max_reels)
    rows = [reel_row(i) for i in items if i.get("shortCode") and not i.get("error")]
    rows.sort(key=lambda r: r["taken_at"] or "", reverse=True)  # newest first, like the Reels tab
    if not rows and items:
        raise _no_data(items, f"@{username}")
    for r in rows:
        ps.log_reel(r["shortcode"], r["play_count"], r["like_count"], r["comment_count"])
    target = None
    if code:
        match = next((r for r in rows if r["shortcode"] == code), None)
        target = {"shortcode": code, "found": match is not None,
                  "source": "profile" if match else ("reel_link" if target_item else None),
                  "reel": match or (reel_row(target_item) if target_item else None)}
    _say(job, f"Collected {len(rows)} reels from @{username}")
    return {"provider": "apify", "platform": "instagram", "source_url": url.strip(), "username": username,
            "profile_url": f"https://www.instagram.com/{username}/reels/", "target": target, "scrolls": None,
            "reel_count": len(rows),
            "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "reels": rows}
