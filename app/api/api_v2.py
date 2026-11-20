"""SpaceMatch Reel Analytics API v2 - Instagram profile scans (same API keys and conventions as /v1).

POST /v2/profile-scans            {url, max_reels?}    -> 202 {scan}   (runs in the background)
GET  /v2/profile-scans/{id}                           -> scan + data when done
GET  /v2/profile-scans/{id}/json                      -> the collected JSON as a file
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services import scans
from app.api.apikeys import ApiError, require_scope

router = APIRouter(prefix="/v2", tags=["Hub API v2"])


class ScanBody(BaseModel):
    url: str = Field(min_length=10, max_length=2048,
                     description="An Instagram reel link (instagram.com/reel/…) or profile link (instagram.com/<username>/)")
    max_reels: int = Field(default=scans.DEFAULT_REELS, ge=1, le=scans.MAX_REELS,
                           description="How many of the account's latest reels to collect")


def _owned(key: dict, scan_id: str) -> dict:
    row = scans.get_scan(key["owner_id"], scan_id)
    if not row:
        raise ApiError(404, "not_found", "Scan not found")
    return row


def _links(scan_id: str) -> dict:
    return {"self": f"/v2/profile-scans/{scan_id}", "json": f"/v2/profile-scans/{scan_id}/json"}


@router.post("/profile-scans", status_code=202)
def create_scan(body: ScanBody, key: dict = Depends(require_scope("refresh:write"))):
    """Open the reel owner's Instagram Reels tab, scroll, and collect every reel's public counters.
    Answers 202 straight away; poll GET /v2/profile-scans/{id} until status is `done` or `failed`."""
    try:
        scans.validate_url(body.url)
    except scans.ScanError as exc:
        raise ApiError(422, "invalid_link", str(exc), "url")
    row = scans.create_scan(key["owner_id"], body.url, body.max_reels, f"api_key:{key['id']}")
    scans.start_scan(row)
    return {"scan": scans.scan_object(row, include_data=False), "links": _links(row["id"])}


@router.get("/profile-scans/{scan_id}")
def read_scan(scan_id: str, key: dict = Depends(require_scope("socials:read"))):
    row = _owned(key, scan_id)
    return {"scan": scans.scan_object(row), "links": _links(scan_id)}


@router.get("/profile-scans/{scan_id}/json")
def scan_json(scan_id: str, key: dict = Depends(require_scope("socials:read"))):
    row = _owned(key, scan_id)
    if row["status"] != "done":
        raise ApiError(409, "conflict", f"The scan is {scans.scan_object(row)['status']} - JSON is available when it is done")
    text, filename = scans.scan_json(row)
    return Response(text, media_type="application/json", headers={"Content-Disposition": f'inline; filename="{filename}"'})
