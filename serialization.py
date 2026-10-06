"""Serialization helpers: dataclass -> API dict, plus small shared utilities.

Kept together so the JSON output shapes the frontend depends on are defined in
one place. All take the entity (or its id) as an argument and read global
state only when they must (e.g. `current_settings_version`).
"""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from state import FRONTEND, settings
from website import Script, Website

# Fields that are still truly global (baked into every event at creation and
# not overridable per event). These six per-event fields are excluded:
#   website_url, organizer, organizer_first_name, organizer_last_name,
#   organizer_phone_number, organizer_email_address
SETTINGS_GLOBAL_FIELDS = (
    'venue', 'location', 'open_to_public', 'event_cost', 'categories',
    'timezone', 'eventbrite_organization_id', 'eventbrite_categories',
)


def asset_url(name: str) -> str:
    """Cache-busting URL: version = file's last-modified time."""
    path: Path = FRONTEND / name
    mtime = int(path.stat().st_mtime)
    return f'/static/{name}?v={mtime}'


def current_settings_version() -> str:
    """Content hash of the *global* settings; scripts built against an older
    version are stale. Per-event fields (website_url, organizer, names, phone,
    email) are excluded — they live on each Event and are set from the event
    form, so changing their default in Settings doesn't invalidate scripts."""
    partial = {k: getattr(settings, k) for k in SETTINGS_GLOBAL_FIELDS}
    data = json.dumps(partial, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:12]


def _parse_event_id(raw):
    """Coerce a body-provided event id (string) into a UUID, or None if invalid."""
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


def event_out(eid: UUID, event) -> dict:
    return {'id': str(eid), **asdict(event)}


def task_out(task_id: UUID, task) -> dict:
    return {
        'id': str(task_id),
        'event_id': str(task.event_id),
        'url': task.url,
        'status': task.status,
        'created_at': task.created_at.isoformat(),
        'log': task.log,
    }


def script_out(s: Script) -> dict:
    return {
        'id': str(s.id),
        'label': s.label,
        'order': s.order,
        'status': s.status,
        'settings_version': s.settings_version,
        'updated_at': s.updated_at.isoformat(),
        'code': s.code,
    }


def website_out(w: Website, include_log: bool = False) -> dict:
    out = {
        'id': str(w.id),
        'url': w.url,
        'name': w.name,
        'kind': w.kind,
        'status': w.status,
        'phase': w.phase,
        'settings_version': w.settings_version,
        'venue_id': w.venue_id or '',
        'organizer_id': w.organizer_id or '',
        'added_events': [{'event_id': k, 'url': v}
                         for k, v in (w.added_events or {}).items()],
        'stale': bool(w.status == 'scripted' and w.settings_version
                      and w.settings_version != current_settings_version()),
        'created_at': w.created_at.isoformat(),
        'scripts': [script_out(s) for s in sorted(w.scripts, key=lambda x: x.order)],
    }
    if include_log:
        out['log'] = w.log
    return out
