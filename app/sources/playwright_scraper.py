"""Free public Reel/Short counters via a local Playwright browser (no login, no cookies).

YouTube: watch page -> views + likes (player data), scroll -> comments count.
TikTok:  video page (short links redirect) -> plays, likes, comments, shares.


Instagram (tested: 30,630 views on DTOz5t5jLQH)
  1. Open the reel page -> likes, comments and the owner's username.
     Logged-out visitors don't get the view count here.
  2. Open the owner's Reels tab and scroll until the reel's tile loads.
     The grid's network data carries the exact play_count.

Facebook (tested: share link -> 1.79M plays)
  1. Open the reel/share link -> resolves the numeric video id, plus
     reactions/comments/shares. Logged-out visitors don't get views here.
  2. Open /watch/?v=<id> -> its page data has play_count and video_view_count
     tied to the video id.

Uses the *sync* Playwright API so it can run in a worker thread on any OS
(uvicorn's event loop on Windows can't start Playwright subprocesses).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.sync_api import Page, sync_playwright

from urllib.parse import urlparse

from app.services.urls import InvalidReelURL, parse_reel_url

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MAX_SCROLLS = int(os.getenv("PLAYWRIGHT_MAX_SCROLLS", "80"))
IG_COUNT_KEYS = ("play_count", "ig_play_count", "view_count", "video_view_count",
                 "like_count", "comment_count")


class ScrapeError(Exception):
    pass


# ------------------------------------------------------- terminal log ----
# One line per video read, e.g.
#   INFO reel = DTOz5t5jLQH, date + timestamp: 2026-11-18T12:00:05Z, playcount = 30631, like_count = 408, comments_count = 9
reel_log = logging.getLogger("spacematch.reels")
if not reel_log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    reel_log.addHandler(_handler)
    reel_log.setLevel(logging.INFO)
    reel_log.propagate = False


def log_reel(reel_id, playcount, like_count, comments_count):
    show = lambda v: "N/A" if v is None else v  # noqa: E731
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    reel_log.info("reel = %s, date + timestamp: %s, playcount = %s, like_count = %s, comments_count = %s",
                  reel_id, stamp, show(playcount), show(like_count), show(comments_count))


# ---------------------------------------------------------------- jobs ----

@dataclass
class Job:
    id: str
    reel_ids: list
    show_window: bool
    status: str = "running"          # running | done | error
    log: list = field(default_factory=list)
    screenshot: str | None = None    # data: URI of latest frame
    results: list = field(default_factory=list)
    current: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    user_id: str | None = None       # who started it; only they can watch it

    def say(self, msg):
        self.log.append({"t": round(time.time() - self.started_at, 1), "msg": msg})

    def public(self):
        return {"id": self.id, "status": self.status, "log": self.log[-200:],
                "screenshot": self.screenshot, "results": self.results,
                "current": self.current, "show_window": self.show_window,
                "elapsed": round((self.finished_at or time.time()) - self.started_at, 1)}


JOBS: dict[str, Job] = {}
RUN_LOCK = threading.Lock()  # one browser at a time keeps Instagram/Facebook calm


def new_job(reel_ids, show_window, user_id=None):
    job = Job(id=uuid.uuid4().hex[:12], reel_ids=list(reel_ids), show_window=show_window, user_id=user_id)
    JOBS[job.id] = job
    # Keep memory bounded
    for old in sorted(JOBS.values(), key=lambda j: j.started_at)[:-20]:
        JOBS.pop(old.id, None)
    return job


def can_show_window():
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


# ------------------------------------------------------------- browser ----

class Session:
    """Wraps one browser; sends live frames + log lines to the job."""

    def __init__(self, pw, job: Job | None):
        self.job = job
        self.logged: set[str] = set()  # reels already written to the terminal log
        headed = bool(job and job.show_window)
        if headed and not can_show_window():
            self.say("No screen available on this machine - running hidden instead")
            headed = False
        self.browser = pw.chromium.launch(headless=not headed, slow_mo=120 if headed else 0,
                                            args=["--disable-dev-shm-usage"])  # small /dev/shm in containers (Cloud Run)
        self.ctx = self.browser.new_context(user_agent=UA, locale="en-US",
                                            viewport={"width": 1280, "height": 900})

    def say(self, msg):
        if self.job:
            self.job.say(msg)

    def frame(self, page: Page):
        if not self.job:
            return
        try:
            shot = page.screenshot(type="jpeg", quality=55)
            self.job.screenshot = "data:image/jpeg;base64," + base64.b64encode(shot).decode()
        except Exception:
            pass

    def wait(self, page: Page, seconds: float):
        """Wait while still streaming frames to the viewer."""
        end = time.time() + seconds
        while time.time() < end:
            page.wait_for_timeout(min(1000, max(50, int((end - time.time()) * 1000))))
            self.frame(page)

    def open(self, url, capture):
        """New page that records Instagram/Facebook JSON/GraphQL response bodies."""
        page = self.ctx.new_page()

        def on_response(resp):
            u = resp.url
            if any(d in u for d in ("instagram.com", "facebook.com", "youtube.com", "tiktok.com")) and (
                    "graphql" in u or "/api/" in u or "json" in resp.headers.get("content-type", "")):
                try:
                    capture.append(resp.text())
                except Exception:
                    pass

        page.on("response", on_response)
        self.say(f"Opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.frame(page)
        return page

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass


def dismiss_popups(page: Page):
    for sel in ['div[role="dialog"] svg[aria-label="Close"]', 'div[aria-label="Close"]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=300):
                el.click()
                return
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


# ----------------------------------------------------------- instagram ----

def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _json_docs(texts):
    for t in texts:
        t = t.removeprefix("for (;;);")
        try:
            yield json.loads(t)
        except (json.JSONDecodeError, TypeError):
            continue


def _script_json(html):
    return [m.group(1) for m in re.finditer(
        r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S)]


def ig_media_index(texts):
    """shortcode -> merged counts (+ owner username) from any JSON we saw."""
    found = {}
    for doc in _json_docs(texts):
        for d in _walk(doc):
            code = d.get("code") or d.get("shortcode")
            if not isinstance(code, str) or not any(k in d for k in IG_COUNT_KEYS):
                continue
            entry = found.setdefault(code, {})
            for k in IG_COUNT_KEYS:
                if d.get(k) is not None:
                    entry.setdefault(k, d[k])
            owner = d.get("user") or d.get("owner") or {}
            if isinstance(owner, dict) and owner.get("username"):
                entry.setdefault("username", owner["username"])
    return found


def _ig_views(entry):
    for k in ("play_count", "ig_play_count", "view_count", "video_view_count"):
        if isinstance(entry.get(k), int):
            return entry[k]
    return None


def parse_compact(text):
    """'30.6K' -> 30600, '1,234' -> 1234."""
    t = text.strip().replace(",", "").upper()
    mult = {"K": 1_000, "M": 1_000_000}.get(t[-1:], 1)
    try:
        return int(float(t.rstrip("KM")) * mult)
    except ValueError:
        return None


def log_seen_reels(s: Session, captured, final: bool = False):
    """Log each reel found in the captured Instagram data once - as soon as its play count is known,
    or (final=True) with whatever counts were found."""
    for code, entry in ig_media_index(captured).items():
        if code in s.logged or (_ig_views(entry) is None and not final):
            continue
        s.logged.add(code)
        log_reel(code, _ig_views(entry), entry.get("like_count"), entry.get("comment_count"))


def scrape_instagram(s: Session, url, shortcode):
    captured = []
    page = s.open(url, captured)
    s.wait(page, 6)
    captured += _script_json(page.content())
    entry = ig_media_index(captured).get(shortcode, {})
    username = entry.get("username")
    if not username:
        m = re.search(r'"username":"([\w.]+)"', page.content())
        username = m.group(1) if m else None
    s.say(f"Reel page: likes={entry.get('like_count')}, comments={entry.get('comment_count')}, owner=@{username}")
    page.close()

    views = _ig_views(entry)
    scrolls = 0
    if views is None:
        if not username:
            raise ScrapeError("Could not find the reel owner's username (reel may be private or removed)")
        page = s.open(f"https://www.instagram.com/{username}/reels/", captured)
        s.wait(page, 5)
        captured += _script_json(page.content())
        link = page.locator(f'a[href*="/reel/{shortcode}"]')
        last_h, stuck = 0, 0
        while scrolls < MAX_SCROLLS:
            entry = {**ig_media_index(captured).get(shortcode, {}), **entry}
            if _ig_views(entry) is not None or link.count():
                break
            dismiss_popups(page)
            page.mouse.wheel(0, 2500)
            s.wait(page, 1.8)
            scrolls += 1
            h = page.evaluate("document.body.scrollHeight")
            stuck = stuck + 1 if h == last_h else 0
            last_h = h
            s.say(f"Scroll {scrolls}: {len(ig_media_index(captured))} reels loaded")
            log_seen_reels(s, captured)
            if stuck >= 5:
                s.say("Reels tab stopped loading more")
                break
        merged = ig_media_index(captured).get(shortcode, {})
        views = _ig_views(merged)
        for k in ("like_count", "comment_count"):
            entry.setdefault(k, merged.get(k))
        if views is None and link.count():
            # Fallback: rounded number on the tile (e.g. "30.6K")
            link.first.scroll_into_view_if_needed()
            nums = re.findall(r"[\d.,]+[KM]?", link.first.inner_text())
            views = parse_compact(nums[-1]) if nums else None
            s.say(f"Read rounded view count from tile: {nums[-1] if nums else 'none'}")
        s.frame(page)
        page.close()
        if views is None:
            s.say(f"Reel not reached in Reels tab after {scrolls} scrolls")

    if shortcode not in s.logged:  # the reel we came for, with its final numbers
        s.logged.add(shortcode)
        log_reel(shortcode, views, entry.get("like_count"), entry.get("comment_count"))
    log_seen_reels(s, captured, final=True)  # everything else scrolled past
    return {"views": views, "likes": entry.get("like_count"),
            "comments": entry.get("comment_count"), "shares": None,
            "detail": f"Instagram public pages via Playwright (reel page + @{username} Reels tab, {scrolls} scrolls)"}


# ------------------------------------------------------------ facebook ----

def _num(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _near(pattern, blob, keys, radius=2000):
    for k in keys:
        if not k:
            continue
        for m in re.finditer(re.escape(k), blob):
            v = _num(pattern, blob[max(0, m.start() - radius): m.end() + radius])
            if v is not None:
                return v
    return None


def fb_views_from_watch(blob, video_id):
    """Find the view-count block tied to this video id. Returns (dict, feedback_id)."""
    anchor = re.search(r'"associated_video":\{[^{}]*"id":"%s"\}' % re.escape(video_id), blob)
    if not anchor:
        return {}, None
    block = blob[anchor.end(): anchor.end() + 800]
    fid = re.search(r'"id":"([A-Za-z0-9=_-]{20,})"', block)
    return {"play_count": _num(r'"play_count":(\d+)', block),
            "video_view_count": _num(r'"video_view_count":(\d+)', block)}, (fid.group(1) if fid else None)


def scrape_facebook(s: Session, url):
    reel_blob = []
    page = s.open(url, reel_blob)
    s.wait(page, 6)
    dismiss_popups(page)
    final = page.url
    reel_blob = page.content() + "\n".join(reel_blob)
    page.close()
    m = re.search(r"/(?:reel|videos)/(\d+)", final) or re.search(r"[?&]v=(\d+)", final)
    if not m:
        raise ScrapeError(f"Could not find a Facebook video id (landed on {final})")
    video_id = m.group(1)
    s.say(f"Resolved video id {video_id}")

    watch_blob = []
    page = s.open(f"https://www.facebook.com/watch/?v={video_id}", watch_blob)
    s.wait(page, 6)
    watch_blob = page.content() + "\n".join(watch_blob)
    page.close()

    views, fid = fb_views_from_watch(watch_blob, video_id)
    s.say(f"Watch page: plays={views.get('play_count')}, views={views.get('video_view_count')}")
    keys = [fid, video_id]
    result = {
        "views": views.get("play_count") or views.get("video_view_count"),
        "likes": _near(r'"reaction_count":\{"count":(\d+)', watch_blob, [fid])
                 or _near(r'"unified_reactors":\{"count":(\d+)', reel_blob, keys),
        "comments": _near(r'"total_comment_count":(\d+)', watch_blob, [fid])
                    or _near(r'"total_comment_count":(\d+)', reel_blob, keys),
        "shares": _near(r'"share_count_reduced":"(\d+)"', reel_blob, keys),
        "detail": (f"Facebook public pages via Playwright (video {video_id}; views = plays"
                   + (f", stricter views={views['video_view_count']:,}" if views.get("video_view_count") else "") + ")"),
    }
    return result


# ------------------------------------------------------------- youtube ----

def _int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def yt_counts(blob, video_id):
    """Views + likes from the player microformat tied to this video id;
    comments from the comments header (absent = turned off/not loaded)."""
    views = likes = comments = None
    m = re.search(r'"externalVideoId":"%s"' % re.escape(video_id), blob)
    if m:
        window = blob[max(0, m.start() - 3000): m.end() + 1500]
        views = _num(r'"viewCount":"(\d+)"', window)
        likes = _num(r'"likeCount":"(\d+)"', window)
    if views is None:  # fallback: videoDetails block
        m = re.search(r'"videoDetails":\{"videoId":"%s".{0,4000}?"viewCount":"(\d+)"' % re.escape(video_id), blob, re.S)
        views = int(m.group(1)) if m else None
    m = re.search(r'"commentsHeaderRenderer":\{"countText":\{"runs":\[\{"text":"([\d,.]+)"', blob)
    if m:
        comments = _int(m.group(1))
    return {"views": views, "likes": likes, "comments": comments}


def scrape_youtube(s: Session, video_id):
    """Watch page carries views/likes; scrolling loads the comments header."""
    captured = []
    page = s.open(f"https://www.youtube.com/watch?v={video_id}", captured)
    s.wait(page, 4)
    for sel in ['button:has-text("Reject all")', 'button:has-text("Accept all")']:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click()
                s.wait(page, 2)
                break
        except Exception:
            pass
    counts = yt_counts(page.content(), video_id)
    s.say(f"Video page: views={counts['views']}, likes={counts['likes']}")
    for i in range(8):  # comments load lazily below the player
        page.mouse.wheel(0, 900)
        s.wait(page, 1.2)
        found = yt_counts(page.content() + "\n".join(captured), video_id)["comments"]
        if found is not None:
            counts["comments"] = found
            break
    s.say(f"Comments: {counts['comments'] if counts['comments'] is not None else 'not shown (turned off or hidden)'}")
    page.close()
    return {**counts, "shares": None,
            "detail": f"YouTube public watch page via Playwright (video {video_id}"
                      + ("; comments turned off or hidden" if counts["comments"] is None else "") + ")"}


# -------------------------------------------------------------- tiktok ----

def tt_stats(html):
    """Stats from TikTok's embedded page data. Returns (stats_dict, video_id, author)."""
    m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return {}, None, None
    try:
        data = json.loads(m.group(1))
        item = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}, None, None
    stats = item.get("statsV2") or item.get("stats") or {}
    return {k: _int(str(v)) for k, v in stats.items()}, item.get("id"), (item.get("author") or {}).get("uniqueId")


def scrape_tiktok(s: Session, url):
    captured = []
    page = s.open(url, captured)
    s.wait(page, 6)
    final = page.url
    stats, vid, author = tt_stats(page.content())
    page.close()
    if not vid:
        raise ScrapeError(f"TikTok page had no video data (landed on {final.split('?')[0]}); it may be private or blocked")
    s.say(f"Video {vid} by @{author}: plays={stats.get('playCount')}, likes={stats.get('diggCount')}")
    return {"views": stats.get("playCount"), "likes": stats.get("diggCount"),
            "comments": stats.get("commentCount"), "shares": stats.get("shareCount"),
            "embed_url": f"https://www.tiktok.com/embed/v2/{vid}",
            "detail": (f"TikTok public page via Playwright (@{author} video {vid}; saves="
                       f"{stats.get('collectCount')})")}


# --------------------------------------------------------------- entry ----

def scrape(url, job: Job | None = None, pw=None):
    """Return {'views','likes','comments','shares','detail'} for one public reel URL."""
    parsed = parse_reel_url(url)
    if parsed["platform"] == "instagram":
        from app.sources import apify_source  # Instagram through Apify when configured; the browser is the fallback
        if apify_source.enabled():
            try:
                return apify_source.instagram_reel(parsed["url"], job)
            except apify_source.ApifyError as exc:
                if job:
                    job.say(f"Apify unavailable ({exc}) - falling back to the browser")
    if parsed["platform"] == "youtube":
        from app.sources import youtube_api  # official API when configured; the browser is the fallback
        if youtube_api.enabled():
            try:
                result = youtube_api.video_stats(parsed["key"], job)
                log_reel(parsed["key"], result["views"], result["likes"], result["comments"])
                return result
            except youtube_api.YouTubeApiError as exc:
                if job:
                    job.say(f"YouTube API unavailable ({exc}) - falling back to the browser")
    own_pw = pw is None
    if own_pw:
        pw = sync_playwright().start()
    s = Session(pw, job)
    try:
        platform = parsed["platform"]
        if platform == "instagram":
            return scrape_instagram(s, parsed["url"], parsed["key"])
        if platform == "youtube":
            result = scrape_youtube(s, parsed["key"])
        elif platform == "tiktok":
            result = scrape_tiktok(s, parsed["url"])
        else:
            result = scrape_facebook(s, parsed["url"])
        m = re.search(r"video ([\w-]+)", result.get("detail") or "")  # resolved id (share links)
        log_reel(m.group(1) if m else parsed["key"], result.get("views"), result.get("likes"), result.get("comments"))
        return result
    finally:
        s.close()
        if own_pw:
            pw.stop()


# ------------------------------------------------------- profile scan ----
# Open the owner's Reels tab, scroll, and return every reel's public counters as JSON.

IG_RESERVED_PATHS = {"reel", "reels", "p", "explore", "stories", "accounts", "tv", "direct", "about", "legal"}


def parse_instagram_target(url: str) -> tuple[str | None, str | None]:
    """(username, shortcode) from an Instagram reel or profile link. Raises InvalidReelURL otherwise."""
    if not isinstance(url, str) or len(url) > 2048:
        raise InvalidReelURL("Paste an Instagram reel or profile link")
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if u.scheme != "https" or host not in {"instagram.com", "www.instagram.com"} or u.username or u.port:
        raise InvalidReelURL("Profile scans work with Instagram links (https://www.instagram.com/...) only")
    profile = re.fullmatch(r"/([A-Za-z0-9_.]{1,30})(?:/reels)?/?", u.path)
    if profile and profile.group(1).lower() not in IG_RESERVED_PATHS:
        return profile.group(1), None
    shortcode = parse_reel_url(url)["key"]  # raises for anything that isn't a reel
    owner = re.match(r"/([A-Za-z0-9_.]{1,30})/reels?/", u.path)
    username = owner.group(1) if owner and owner.group(1).lower() not in IG_RESERVED_PATHS else None
    return username, shortcode


def ig_profile_reels(texts) -> dict:
    """shortcode -> counts, post time and owner from the captured Instagram JSON (grid order)."""
    found = {}
    for doc in _json_docs(texts):
        for d in _walk(doc):
            code = d.get("code") or d.get("shortcode")
            if not isinstance(code, str) or not any(k in d for k in IG_COUNT_KEYS):
                continue
            entry = found.setdefault(code, {})
            for k in (*IG_COUNT_KEYS, "taken_at"):
                if d.get(k) is not None:
                    entry.setdefault(k, d[k])
            owner = d.get("user") or d.get("owner") or {}
            if isinstance(owner, dict) and owner.get("username"):
                entry.setdefault("username", owner["username"])
    return found


def _utc_iso(ts) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def scan_instagram_profile(url: str, job: Job | None = None, pw=None, max_scrolls: int = 10) -> dict:
    """Go to the profile's Reels tab, scroll up to `max_scrolls` times, collect every reel's JSON data."""
    username, shortcode = parse_instagram_target(url)
    own_pw = pw is None
    if own_pw:
        pw = sync_playwright().start()
    s = Session(pw, job)
    try:
        page_data = []  # the pasted reel's own page (likes/comments/owner, no plays)
        captured = []   # the Reels tab grid
        if shortcode:  # the reel page tells us who posted it, and its likes/comments
            page = s.open(f"https://www.instagram.com/reel/{shortcode}/", page_data)
            s.wait(page, 5)
            page_data += _script_json(page.content())
            username = username or ig_media_index(page_data).get(shortcode, {}).get("username")
            if not username:
                m = re.search(r'"username":"([\w.]+)"', page.content())
                username = m.group(1) if m else None
            page.close()
            if not username:
                raise ScrapeError("Could not find the reel owner's username (reel may be private or removed)")
        profile_url = f"https://www.instagram.com/{username}/reels/"
        s.say(f"Opening @{username} Reels tab")
        page = s.open(profile_url, captured)
        s.wait(page, 5)
        captured += _script_json(page.content())
        scrolls, stuck, last_h = 0, 0, 0
        while scrolls < max_scrolls:
            dismiss_popups(page)
            page.mouse.wheel(0, 2500)
            s.wait(page, 1.8)
            scrolls += 1
            h = page.evaluate("document.body.scrollHeight")
            stuck = stuck + 1 if h == last_h else 0
            last_h = h
            s.say(f"Scroll {scrolls}/{max_scrolls}: {len(ig_profile_reels(captured))} reels collected")
            log_seen_reels(s, captured)
            if stuck >= 5:
                s.say("Reels tab stopped loading more")
                break
        s.frame(page)
        captured += _script_json(page.content())
        page.close()
        log_seen_reels(s, captured, final=True)

        reels = []
        for code, e in ig_profile_reels(captured).items():
            if e.get("username") and e["username"].lower() != username.lower():
                continue  # suggested reels from other accounts
            reels.append({"shortcode": code, "url": f"https://www.instagram.com/reel/{code}/",
                          "play_count": _ig_views(e), "like_count": e.get("like_count"),
                          "comment_count": e.get("comment_count")})
        target = None
        if shortcode:  # found = present in the Reels tab grid (with its play count)
            match = next((r for r in reels if r["shortcode"] == shortcode), None)
            page_entry = ig_profile_reels(page_data).get(shortcode, {})
            page_reel = {"shortcode": shortcode, "url": f"https://www.instagram.com/reel/{shortcode}/",
                         "play_count": _ig_views(page_entry), "like_count": page_entry.get("like_count"),
                         "comment_count": page_entry.get("comment_count"),
                         "taken_at": _utc_iso(page_entry.get("taken_at"))} if page_entry else None
            target = {"shortcode": shortcode, "found": match is not None,
                      "source": "reels_tab" if match else ("reel_page" if page_reel else None),
                      "reel": match or page_reel}
        s.say(f"Collected {len(reels)} reels from @{username}" +
              (f"; pasted reel {'found' if target['found'] else 'not reached'} in the Reels tab" if target else ""))
        return {"provider": "browser", "platform": "instagram", "source_url": url.strip(), "username": username,
                "profile_url": profile_url,
                "target": target, "scrolls": scrolls, "reel_count": len(reels),
                "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "reels": reels}
    finally:
        s.close()
        if own_pw:
            pw.stop()
