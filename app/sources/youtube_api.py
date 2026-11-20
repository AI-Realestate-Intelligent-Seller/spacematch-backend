"""YouTube counters through the official YouTube Data API v3 (videos.list, part=statistics).

Used when YOUTUBE_API_KEY is set (a plain Google Cloud API key - no login). One lookup costs 1 quota unit
of the free 10,000/day. YouTubeApiError (bad key, quota, network) lets the caller fall back to the browser;
ScrapeError means the video doesn't exist or isn't public.
"""
from __future__ import annotations

import httpx

from app.sources import playwright_scraper as ps
from app.core.settings import YOUTUBE_API_KEY

API = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeApiError(Exception):
    """The API itself failed - the caller may fall back to the browser."""


def enabled() -> bool:
    return bool(YOUTUBE_API_KEY)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None  # hidden likes / disabled comments are simply absent


def video_stats(video_id: str, job=None) -> dict:
    """Same result shape as playwright_scraper.scrape()."""
    if job:
        job.say(f"Reading YouTube video {video_id} via the YouTube Data API")
    try:
        r = httpx.get(API, params={"part": "statistics", "id": video_id},
                      headers={"X-goog-api-key": YOUTUBE_API_KEY}, timeout=20)
    except httpx.HTTPError as exc:
        raise YouTubeApiError(f"YouTube API request failed ({type(exc).__name__})") from exc
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason") or err.get("message") or ""
        except ValueError:
            reason = ""
        raise YouTubeApiError(f"YouTube API error {r.status_code}: {reason}".strip(": "))
    items = r.json().get("items") or []
    if not items:
        raise ps.ScrapeError("YouTube video not found - it may be private or removed")
    stats = items[0].get("statistics") or {}
    result = {"views": _int(stats.get("viewCount")), "likes": _int(stats.get("likeCount")),
              "comments": _int(stats.get("commentCount")), "shares": None,
              "detail": f"YouTube Data API v3 (video {video_id})"}
    if job:
        job.say(f"YouTube API: views={result['views']}, likes={result['likes']}, comments={result['comments']}")
    return result
