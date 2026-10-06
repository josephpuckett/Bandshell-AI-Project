"""Eventbrite API client (no LLM, no browser).

Used by Eventbrite-kind websites to create drafts from an `Event`. Publishing
stays manual: the human reviews the draft in Eventbrite and publishes there.

The token (EVENTBRITE_TOKEN) is read from state and never exposed to the UI
or persisted in sheets.
"""
import json
import logging
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import httpx
import requests

from event import Event
from state import EVENTBRITE_TOKEN, settings

API = "https://www.eventbriteapi.com/v3"

log = logging.getLogger("eventbrite")


def _headers():
    return {"Authorization": f"Bearer {EVENTBRITE_TOKEN}"}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def resolve_organization_id() -> str:
    """Return the Eventbrite organization id: settings override or first org from the token."""
    if settings.eventbrite_organization_id:
        return settings.eventbrite_organization_id
    r = requests.get(f"{API}/users/me/organizations/", headers=_headers(), timeout=30)
    r.raise_for_status()
    orgs = r.json().get("organizations") or []
    if not orgs:
        raise RuntimeError("no Eventbrite organizations found for this token")
    return str(orgs[0]["id"])


def list_categories() -> list[dict]:
    r = requests.get(f"{API}/categories/", headers=_headers(), timeout=30)
    r.raise_for_status()
    return [{"id": str(c["id"]), "name": c["name"], "short_name": c.get("short_name")}
            for c in r.json().get("categories", [])]


# ---------------------------------------------------------------------------
# Date / image helpers
# ---------------------------------------------------------------------------
def _to_utc(dt: datetime, tz_name: str) -> str:
    """Convert a naive local datetime to an Eventbrite UTC string."""
    tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drive_file_id(url: str) -> str | None:
    """Extract the file id from a Google Drive share link, if any."""
    host = (urlparse(url).netloc or "").lower()
    if "drive.google.com" not in host and "drive.usercontent.google.com" not in host:
        return None
    # /file/d/{id}/view?usp=...
    m = re.search(r"/file/d/([^/]+)", url)
    if m:
        return m.group(1)
    # id=... query param
    q = parse_qs(urlparse(url).query)
    if q.get("id"):
        return q["id"][0]
    return None


def direct_image_url(url: str) -> str:
    """Rewrite a Google Drive link to a directly-downloadable URL.

    Pure string transformation (no download, so it's safe to call anytime):
    share-link forms like /file/d/{id}/view or open?id=... become
    https://drive.google.com/uc?export=download&id={id}, which most sites can
    load directly. Non-Drive URLs pass through unchanged. Empty stays empty.
    """
    if not url:
        return url
    fid = _drive_file_id(url)
    if not fid:
        return url
    return f"https://drive.google.com/uc?export=download&id={fid}"


def check_direct_image_url(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Best-effort check that a URL actually serves image bytes, not HTML.

    Used at onboarding time to catch a Drive file that isn't publicly shared:
    Google returns an HTML sign-in/request-access page for unshared files, and
    the calendar site later bails with "got HTML instead of an image". Returns
    (ok, reason). Never raises; failures just report the problem.
    """
    if not url:
        return False, 'image URL is empty'
    fid = _drive_file_id(url)
    if fid:
        # Drive link: check the rewritten direct URL (same one the agent fills).
        candidates = [direct_image_url(url)]
    else:
        candidates = [url]
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for cand in candidates:
                try:
                    r = client.head(cand)
                    ctype = (r.headers.get('content-type') or '').lower()
                    if 'html' in ctype:
                        return False, 'Google served an HTML page — the file may not be shared with "Anyone with the link"'
                    if r.status_code >= 400:
                        continue
                    if 'image' in ctype:
                        return True, 'ok'
                    # Content-type unclear — sniff the first bytes.
                    rr = client.get(cand)
                    head = rr.content[:16]
                    if (rr.headers.get('content-type') or '').lower().startswith('image') \
                            or head.startswith(b'\xff\xd8') or head.startswith(b'\x89PNG') \
                            or head.startswith(b'GIF8') or (head.startswith(b'RIFF') and b'WEBP' in rr.content[:16]):
                        return True, 'ok'
                    if b'<!DOCTYPE' in rr.content[:512] or b'<html' in rr.content[:512]:
                        return False, 'Google served an HTML page — the file may not be shared with "Anyone with the link"'
                    return False, f'unexpected content-type: {rr.headers.get("content-type")}'
                except Exception:
                    continue
        return False, 'could not fetch the URL to verify it serves an image'
    except Exception as e:
        return False, f'could not verify image URL: {e}'


def _looks_like_image(content_type: str, data: bytes) -> bool:
    """Crude check that the downloaded bytes are actually an image, not HTML."""
    ctype = (content_type or '').lower()
    if 'image' in ctype:
        return True
    head = data[:16]
    if head.startswith(b'\xff\xd8'):  # JPEG
        return True
    if head.startswith(b'\x89PNG'):  # PNG
        return True
    if head.startswith(b'GIF8'):
        return True
    if head.startswith(b'RIFF'):  # WEBP (RIFF....WEBP)
        return b'WEBP' in data[:16]
    return False



def _download_drive(fid: str) -> tuple[bytes | None, str]:
    """Download a file from Google Drive by id, trying several reliable endpoints.

    Returns (image_bytes, content_type) or (None, '') if every attempt yields
    HTML/an error. Drive's `uc?export=download` can hand back a virus-scan page;
    the usercontent + thumbnail endpoints are more binary-friendly.
    """
    candidates = [
        f"https://drive.usercontent.google.com/download?id={fid}&export=download",
        f"https://drive.google.com/uc?export=download&id={fid}",
        f"https://drive.google.com/thumbnail?id={fid}&sz=w1600",
    ]
    for url in candidates:
        try:
            r = requests.get(url, timeout=60, allow_redirects=True)
            if r.status_code >= 400:
                continue
            ctype = r.headers.get("content-type", "") or ""
            # If head looks binary / is an image, accept it.
            if _looks_like_image(ctype, r.content):
                return r.content, (ctype if 'image' in ctype.lower() else "image/jpeg")
            # Fallback: parse a confirm token from a virus-scan page and retry uc.
            m = re.search(r'name="confirm" value="([^"]+)"', r.text)
            if m and url.startswith("https://drive.google.com/uc?"):
                retry = (f"https://drive.google.com/uc?export=download&id={fid}"
                         f"&confirm={m.group(1)}")
                r2 = requests.get(retry, timeout=60, allow_redirects=True)
                if _looks_like_image(r2.headers.get("content-type", ""), r2.content):
                    return r2.content, "image/jpeg"
        except Exception:
            continue
    return None, ""


def fetch_image_bytes(url: str) -> tuple[bytes | None, str]:
    """Download image bytes from a (possibly Google Drive) URL.

    Returns (bytes, content_type) or (None, '') on failure.
    """
    fid = _drive_file_id(url)
    if fid:
        return _download_drive(fid)
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "") or "image/jpeg"
        if not _looks_like_image(ctype, r.content):
            return None, ""
        return r.content, ctype
    except Exception as e:
        log.warning("image download failed for %s: %s", url, e)
        return None, ""


def upload_image(data: bytes, content_type: str) -> str | None:
    """Upload image bytes to Eventbrite media, returning the media id (or None).

    Implements Eventbrite's documented 3-step flow:
      1. GET /media/upload/?type=image-event-logo  -> instructions + token
      2. POST upload_url with upload_data form fields + the image file
      3. POST /media/upload/ with {upload_token, crop_mask} -> media id
    Fails open (returns None) and logs diagnostics.
    """
    try:
        # 1. Retrieve upload instructions + token (type is a QUERY param).
        r = requests.get(f"{API}/media/upload/", headers=_headers(),
                         params={"type": "image-event-logo"}, timeout=30)
        if r.status_code >= 400:
            log.warning("media/upload instructions failed (%s): %s",
                        r.status_code, r.text[:300])
            return None
        up = r.json()
        upload_token = up.get("upload_token")
        upload_url = up.get("upload_url")
        upload_data = up.get("upload_data") or {}
        file_param = up.get("file_parameter_name", "file")
        if not (upload_token and upload_url):
            log.warning("media/upload instructions missing token/url: %s", up)
            return None

        # 2. Upload the image bytes to the pre-signed URL.
        r2 = requests.post(upload_url, data=upload_data,
                           files={file_param: ("image", data,
                                               content_type or "image/jpeg")},
                           timeout=60)
        if r2.status_code >= 400:
            log.warning("media upload push failed (%s): %s",
                        r2.status_code, r2.text[:300])
            return None

        # 3. Notify the API. A crop_mask (2:1 ratio) is required to avoid the
        #    legacy listing layout.
        r3 = requests.post(f"{API}/media/upload/", headers=_headers(),
                           json={
                               "upload_token": upload_token,
                               "crop_mask": {"top_left": {"y": 1, "x": 1},
                                             "width": 10000, "height": 5000},
                           },
                           timeout=30)
        if r3.status_code >= 400:
            log.warning("media finalize failed (%s): %s",
                        r3.status_code, r3.text[:300])
            return None
        return str(r3.json()["id"])
    except Exception as e:
        log.warning("eventbrite image upload failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------
def create_event(event: Event, logo_id: str | None = None) -> dict:
    """Create a DRAFT Eventbrite event from an Event. Returns the API response dict.

    Raises on API failure. The event is not published — the human publishes it.
    If a `logo_id` is provided (from a prior media upload), it is attached at
    creation.
    """
    org_id = resolve_organization_id()
    tz = settings.timezone

    payload: dict = {
        "event": {
            "name": {"html": event.title},
            "description": {"html": event.long_summary or event.short_summary},
            "start": {"timezone": tz, "utc": _to_utc(event.start_date, tz)},
            "end": {"timezone": tz, "utc": _to_utc(event.end_date, tz)},
            "currency": "USD",
            "listed": bool(event.open_to_public),
            "online_event": False,
        }
    }
    if logo_id:
        payload["event"]["logo_id"] = str(logo_id)
    # venue/organizer ids come from the website config, passed in as attributes
    vid = getattr(event, "_eventbrite_venue_id", "")
    oid = getattr(event, "_eventbrite_organizer_id", "")
    if vid:
        payload["event"]["venue_id"] = vid
    if oid:
        payload["event"]["organizer_id"] = oid

    # category id from settings map, if any event category has a mapping
    for cat in event.categories:
        cid = settings.eventbrite_categories.get(str(cat))
        if cid:
            payload["event"]["category_id"] = cid
            break

    r = requests.post(f"{API}/organizations/{org_id}/events/",
                      headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Eventbrite create failed ({r.status_code}): {r.text[:500]}")
    return r.json()


def add_free_ticket_class(eventbrite_event_id: str) -> None:
    """Add a free 'General Admission' ticket class (events are always free)."""
    # A free, non-donation class needs a quantity_total. Currency lives on the
    # event, so it is not set here.
    payload = {
        "ticket_class": {
            "name": "General Admission",
            "free": True,
            "quantity_total": 2000,
        }
    }
    r = requests.post(f"{API}/events/{eventbrite_event_id}/ticket_classes/",
                      headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Eventbrite ticket class failed ({r.status_code}): {r.text[:500]}")