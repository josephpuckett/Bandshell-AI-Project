"""Google Sheets persistence via gspread.

Enabled when GOOGLE_SERVICE_ACCOUNT_JSON (JSON content or path to a JSON file)
and EVENTS_SPREADSHEET_ID are set in the environment.

Three worksheets: Events, Websites, Settings. Each is a header row + one row
per entity; nested data (categories, scripts, added_events, settings) is
JSON-encoded in a single cell. Loads hydrate the in-memory registries in
`state`; saves snapshot them back (full-sheet replace — fine for demo scale).

Tasks and website/task logs stay memory-only for now.
"""
import asyncio
import json
import os
from datetime import datetime
from uuid import UUID

import gspread

import state
from event import Event
from settings import Settings
from website import Script, Website

EVENT_COLS = [
    "id", "title", "short_summary", "long_summary", "categories", "image",
    "start_date", "end_date", "venue", "location", "open_to_public",
    "event_cost", "website_url", "organizer", "organizer_first_name",
    "organizer_last_name", "organizer_phone_number", "organizer_email_address",
]
WEBSITE_COLS = [
    "id", "url", "name", "kind", "status", "phase", "settings_version",
    "venue_id", "organizer_id", "added_events", "scripts", "created_at",
]


class DriveStorage:
    def __init__(self):
        raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        if raw.lstrip().startswith("{"):
            creds = json.loads(raw)
            self.gc = gspread.service_account_from_dict(creds)
        else:
            self.gc = gspread.service_account(raw)  # path to the JSON file
        self.sh = self.gc.open_by_key(os.environ["EVENTS_SPREADSHEET_ID"])

    # ------------------------------------------------------------------ load
    async def load(self):
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self):
        self._load_events()
        self._load_websites()
        self._load_settings()

    def _load_events(self):
        state.events.clear()
        try:
            rows = self.sh.worksheet("Events").get_all_records()
        except gspread.WorksheetNotFound:
            return
        for r in rows:
            try:
                ev = Event(
                    title=r["title"],
                    short_summary=r.get("short_summary", ""),
                    long_summary=r.get("long_summary", ""),
                    image=r.get("image", ""),
                    start_date=datetime.fromisoformat(r["start_date"]),
                    end_date=datetime.fromisoformat(r["end_date"]),
                    venue=r.get("venue", ""),
                    location=r.get("location", ""),
                    open_to_public=bool(r.get("open_to_public", True)),
                    event_cost=float(r.get("event_cost", 0.0)),
                    website_url=r.get("website_url", ""),
                    organizer=r.get("organizer", ""),
                    organizer_first_name=r.get("organizer_first_name", ""),
                    organizer_last_name=r.get("organizer_last_name", ""),
                    organizer_phone_number=r.get("organizer_phone_number", ""),
                    organizer_email_address=r.get("organizer_email_address", ""),
                    categories=json.loads(r.get("categories", "[]")),
                )
                state.events[UUID(r["id"])] = ev
            except Exception:
                continue

    def _load_websites(self):
        state.websites.clear()
        try:
            rows = self.sh.worksheet("Websites").get_all_records()
        except gspread.WorksheetNotFound:
            return
        for r in rows:
            try:
                w = Website(url=r["url"], name=r.get("name", r["url"]))
                w.id = UUID(r["id"])
                w.kind = r.get("kind", "agentic")
                w.status = r.get("status", "unscripted")
                w.phase = r.get("phase", "idle")
                w.settings_version = r.get("settings_version", "")
                w.venue_id = r.get("venue_id", "")
                w.organizer_id = r.get("organizer_id", "")
                w.added_events = json.loads(r.get("added_events", "{}"))
                try:
                    w.created_at = datetime.fromisoformat(r["created_at"])
                except (KeyError, ValueError):
                    pass
                w.scripts = []
                for s in json.loads(r.get("scripts", "[]")):
                    w.scripts.append(Script(
                        id=UUID(s["id"]),
                        label=s.get("label", "enter_fields"),
                        order=s.get("order", 0),
                        code=s.get("code", ""),
                        status=s.get("status", "draft"),
                        settings_version=s.get("settings_version", ""),
                        updated_at=datetime.fromisoformat(s["updated_at"]),
                    ))
                state.websites[w.id] = w
            except Exception:
                continue

    def _load_settings(self):
        try:
            rows = self.sh.worksheet("Settings").get_all_records()
        except gspread.WorksheetNotFound:
            return
        if rows and "data" in rows[0]:
            try:
                data = json.loads(rows[0]["data"])
                for f in ("venue", "location", "open_to_public", "event_cost",
                          "website_url", "organizer", "organizer_first_name",
                          "organizer_last_name", "organizer_phone_number",
                          "organizer_email_address", "categories", "timezone",
                          "eventbrite_organization_id", "eventbrite_categories"):
                    if f in data:
                        setattr(state.settings, f, data[f])
            except Exception:
                pass

    # ------------------------------------------------------------------ save
    async def save(self):
        await asyncio.to_thread(self._save_sync)

    def _save_sync(self):
        self._save_events()
        self._save_websites()
        self._save_settings()

    def _save_events(self):
        ws = self._ensure("Events", EVENT_COLS)
        rows = [EVENT_COLS]
        for eid, ev in state.events.items():
            d = {
                "id": str(eid),
                "title": ev.title,
                "short_summary": ev.short_summary,
                "long_summary": ev.long_summary,
                "categories": json.dumps(ev.categories),
                "image": ev.image,
                "start_date": ev.start_date.isoformat(),
                "end_date": ev.end_date.isoformat(),
                "venue": ev.venue,
                "location": ev.location,
                "open_to_public": ev.open_to_public,
                "event_cost": ev.event_cost,
                "website_url": ev.website_url,
                "organizer": ev.organizer,
                "organizer_first_name": ev.organizer_first_name,
                "organizer_last_name": ev.organizer_last_name,
                "organizer_phone_number": ev.organizer_phone_number,
                "organizer_email_address": ev.organizer_email_address,
            }
            rows.append([d.get(c, "") for c in EVENT_COLS])
        ws.clear()
        ws.update(rows, "A1")

    def _save_websites(self):
        ws = self._ensure("Websites", WEBSITE_COLS)
        rows = [WEBSITE_COLS]
        for wid, w in state.websites.items():
            d = {
                "id": str(wid),
                "url": w.url,
                "name": w.name,
                "kind": w.kind,
                "status": w.status,
                "phase": w.phase,
                "settings_version": w.settings_version,
                "venue_id": w.venue_id,
                "organizer_id": w.organizer_id,
                "added_events": json.dumps(w.added_events),
                "scripts": json.dumps([{
                    "id": str(s.id),
                    "label": s.label,
                    "order": s.order,
                    "code": s.code,
                    "status": s.status,
                    "settings_version": s.settings_version,
                    "updated_at": s.updated_at.isoformat(),
                } for s in w.scripts]),
                "created_at": w.created_at.isoformat(),
            }
            rows.append([d.get(c, "") for c in WEBSITE_COLS])
        ws.clear()
        ws.update(rows, "A1")

    def _save_settings(self):
        ws = self._ensure("Settings", ["id", "data"])
        data = {f.name: getattr(state.settings, f.name) for f in Settings.__dataclass_fields__.values()}
        # keep it JSON-friendly
        data = json.loads(json.dumps(data, default=str))
        ws.clear()
        ws.update([["id", "data"], ["default", json.dumps(data)]], "A1")

    def _ensure(self, name: str, cols: list[str]):
        try:
            return self.sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(title=name, rows=10, cols=max(len(cols), 5))
            ws.update([cols], "A1")
            return ws