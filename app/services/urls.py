"""Allowlisted Instagram/Facebook URLs. No fetching arbitrary user-provided hosts."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlparse


class InvalidReelURL(ValueError):
    pass


def parse_reel_url(value: str) -> dict:
    if not isinstance(value, str) or len(value) > 2048:
        raise InvalidReelURL("Please enter a valid Instagram or Facebook Reel link")
    candidate = value.strip()
    url = urlparse(candidate)
    if url.scheme != "https" or url.port is not None or url.username or url.password:
        raise InvalidReelURL("Only HTTPS Instagram or Facebook Reel URLs are accepted")
    host = (url.hostname or "").lower()
    path = url.path.rstrip("/")

    if host in {"instagram.com", "www.instagram.com"}:
        match = re.fullmatch(r"(?:/[\w.]{1,30})?/(?:reel|reels)/([\w-]{3,64})", path)
        if not match:
            raise InvalidReelURL("Use a direct Instagram Reel URL (instagram.com/reel/...) ")
        code = match.group(1)
        return {"platform": "instagram", "key": code,
                "url": f"https://www.instagram.com/reel/{code}/",
                "embed_url": f"https://www.instagram.com/reel/{code}/embed/"}

    if host in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        match = re.fullmatch(r"/reel/(\d{3,32})", path)
        if not match:
            match = re.fullmatch(r"/[^/]+/(?:reels|videos)/(\d{3,32})", path)
        if match:
            code = match.group(1)
            canonical = f"https://www.facebook.com/reel/{code}"
            return {"platform": "facebook", "key": code, "url": canonical,
                    "embed_url": "https://www.facebook.com/plugins/video.php?href=" + quote(canonical, safe="")}
        if path == "/watch":
            v = parse_qs(url.query).get("v", [""])[0]
            if re.fullmatch(r"\d{3,32}", v):
                canonical = f"https://www.facebook.com/watch/?v={v}"
                return {"platform": "facebook", "key": v, "url": canonical,
                        "embed_url": "https://www.facebook.com/plugins/video.php?href=" + quote(canonical, safe="")}
        # Some Facebook Share buttons return opaque short URLs. These can be
        # attached/embedded but cannot be confidently matched to a Page video ID.
        share = re.fullmatch(r"/share/(?:r|v)/([\w-]{3,64})", path)
        if share:
            code = share.group(1)
            canonical = f"https://www.facebook.com/share/r/{code}/" if '/share/r/' in path else f"https://www.facebook.com/share/v/{code}/"
            return {"platform": "facebook", "key": "share:" + code, "url": canonical,
                    "embed_url": "https://www.facebook.com/plugins/video.php?href=" + quote(canonical, safe="")}
        raise InvalidReelURL("Use a direct Facebook Reel or video URL")

    if host == "fb.watch":
        match = re.fullmatch(r"/([\w-]{3,64})", path)
        if match:
            code = match.group(1)
            canonical = f"https://fb.watch/{code}/"
            return {"platform": "facebook", "key": "watch:" + code, "url": canonical,
                    "embed_url": "https://www.facebook.com/plugins/video.php?href=" + quote(canonical, safe="")}
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        vid = None
        if host == "youtu.be":
            m = re.fullmatch(r"/([\w-]{11})", path)
            vid = m and m.group(1)
        else:
            m = re.fullmatch(r"/(?:shorts|live|embed)/([\w-]{11})", path)
            if m:
                vid = m.group(1)
            elif path == "/watch":
                v = parse_qs(url.query).get("v", [""])[0]
                vid = v if re.fullmatch(r"[\w-]{11}", v) else None
        if vid:
            return {"platform": "youtube", "key": vid,
                    "url": f"https://www.youtube.com/shorts/{vid}",
                    "embed_url": f"https://www.youtube.com/embed/{vid}"}
        raise InvalidReelURL("Use a YouTube Shorts or video URL (youtube.com/shorts/...)")

    if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vt.tiktok.com", "vm.tiktok.com"}:
        m = re.fullmatch(r"/@([\w.-]{1,40})/video/(\d{8,25})", path)
        if m:
            vid = m.group(2)
            return {"platform": "tiktok", "key": vid,
                    "url": f"https://www.tiktok.com/@{m.group(1)}/video/{vid}",
                    "embed_url": f"https://www.tiktok.com/embed/v2/{vid}"}
        # Share short links (vt./vm.tiktok.com/CODE, tiktok.com/t/CODE) redirect to the video
        m = re.fullmatch(r"/([\w-]{5,20})", path) if host in {"vt.tiktok.com", "vm.tiktok.com"} \
            else re.fullmatch(r"/t/([\w-]{5,20})", path)
        if m:
            code = m.group(1)
            canonical = (f"https://{host}/{code}/" if host in {"vt.tiktok.com", "vm.tiktok.com"}
                         else f"https://www.tiktok.com/t/{code}/")
            return {"platform": "tiktok", "key": "share:" + code, "url": canonical, "embed_url": None}
        raise InvalidReelURL("Use a TikTok video URL (tiktok.com/@user/video/...) or share link")

    raise InvalidReelURL("Supported: Instagram Reels, Facebook Reels/videos, YouTube Shorts/videos, TikTok videos")
