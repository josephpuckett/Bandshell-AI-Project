"""Bandshell Calendar — FastAPI application (route layer only).

App wiring, lifespan, and HTTP endpoints. Domain logic lives in separate
modules; see `state`, `agents`, `scripting`, `scripter`, `serialization`.
"""
import asyncio
import json
import os
import shutil
from base64 import b64encode
from contextlib import asynccontextmanager
from dataclasses import asdict, fields
from datetime import datetime
from uuid import uuid4, UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agents import run_onboarding, run_task
from browser import (clear_all_live_pages, clear_live_page, get_live_page,
                     manager)
import eventbrite_api
from event import Event
from serialization import (asset_url, current_settings_version, event_out,
                           task_out, website_out, _parse_event_id)
from settings import Settings
import state
from state import (BROWSER_FPS, BROWSER_JPEG_QUALITY, FRONTEND, events,
                   input_queues, runners, settings, tasks, website_queues,
                   website_runners, websites)
from storage import storage
from task import Task
from scripting import run_script_against_event
from website import Website

# Browser contexts left open for the human to review a scripted run and submit
# manually. Keyed by website_id; only one is kept open per website at a time.
review_contexts: dict[UUID, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    # Hydrate in-memory registries from the storage backend (if configured).
    await storage.load()
    # Recreate per-website state (approval/cancel queues) for any sites that
    # were loaded from storage — these are not persisted across sessions.
    for wid in websites:
        website_queues.setdefault(wid, asyncio.Queue())
    # A site left mid-onboarding in the sheets (e.g. the process died while
    # waiting to approve) has NO runner after a restart — its Approve/Reject
    # buttons would post into a queue nobody consumes. Reset it to "not set up"
    # so it doesn't stay stuck. (Shutdown also resets; this covers hard kills.)
    for w in websites.values():
        if w.status == 'scripting':
            w.status = 'unscripted'
            w.phase = 'idle'
            w.scripts = []
            w.settings_version = ''
            w.log.append({'role': 'user',
                          'content': 'Setup was interrupted by a restart; site reset.'})
            storage.mark_dirty()
    storage.start()
    yield
    for t in runners.values():
        t.cancel()
    for t in website_runners.values():
        t.cancel()
    # Close any browser contexts left open for human review of scripted runs.
    for ctx in review_contexts.values():
        try:
            await ctx.close()
        except Exception:
            pass
    clear_all_live_pages()
    # Reset any sites left mid-onboarding so they don't persist a zombie
    # 'scripting' state on the next boot (e.g. when Sheets storage is enabled).
    for w in websites.values():
        if w.status == 'scripting':
            w.status = 'unscripted'
            w.phase = 'idle'
            w.scripts = []
            w.settings_version = ''
            w.log.append({'role': 'user',
                          'content': 'Onboarding was interrupted by server shutdown; site reset.'})
    # Persist any remaining dirty state (including the resets above).
    await storage.stop()


app = FastAPI(lifespan=lifespan)


# ----------------------------- Health ----------------------------------------
@app.get('/api/health')
async def health():
    """Liveness + which optional integrations THIS process can see.

    Reports booleans only (never the values), so it is safe to expose and is the
    quickest way to answer "did my environment file actually load?" — env vars
    are read with os.getenv() at import time, so a shell export or a .env file in
    the repo has no effect on the systemd service.
    """
    return {
        'ok': True,
        'llm_configured': bool(state.TOKEN),
        'llm_model': state.MODEL if state.TOKEN else None,
        'llm_fallbacks': len(state.FALLBACK_MODELS),
        'eventbrite_configured': bool(eventbrite_api.EVENTBRITE_TOKEN),
        'storage_backend': storage.backend,
        'sandbox_mode': state.SCRIPT_SANDBOX,
        'sandbox_bwrap_on_path': bool(shutil.which(state.BWRAP)),
        'headless': os.getenv('HEADLESS') == '1',
        'browser_engine': os.getenv('BROWSER') or 'chromium',
        'events': len(events),
        'websites': len(websites),
    }


# ----------------------------- Settings --------------------------------------
@app.get('/api/settings')
async def get_settings():
    return asdict(settings)


@app.put('/api/settings')
async def update_settings(payload: dict):
    for f in fields(Settings):
        if f.name in payload:
            setattr(settings, f.name, payload[f.name])
    if isinstance(settings.categories, str):
        settings.categories = [c.strip() for c in settings.categories.split(',') if c.strip()]
    if isinstance(settings.eventbrite_categories, str):
        parsed = {}
        for pair in settings.eventbrite_categories.split(','):
            if '=' in pair:
                k, v = pair.split('=', 1)
                parsed[k.strip()] = v.strip()
        settings.eventbrite_categories = parsed
    storage.mark_dirty()
    return asdict(settings)


# Serve the frontend HTML with auto-generated cache-busting asset URLs.
@app.get('/', response_class=HTMLResponse)
async def index():
    html = (FRONTEND / 'index.html').read_text()
    html = html.replace('{{style_url}}', asset_url('style.css'))
    html = html.replace('{{app_url}}', asset_url('app.js'))
    return html


# --------------------------- Storage refresh ---------------------------------
@app.post('/api/refresh')
async def refresh_from_storage():
    """Re-hydrate in-memory state from the storage backend (Google Sheets).

    Lets a human editing the sheet push those changes into the running app
    without a restart. Returns 409 while an AI task/onboarding is in flight,
    because reloading would clobber the website/event objects the runner
    holds references to.
    """
    if storage.backend == 'MemoryStorage':
        return {'refreshed': False, 'backend': storage.backend,
                'message': "Google Sheets isn't enabled — nothing to refresh."}
    active = [t for t in list(runners.values()) + list(website_runners.values())
              if not t.done()]
    if active:
        raise HTTPException(
            409, 'An AI task is running — wait for it to finish (or cancel it) before refreshing.')
    try:
        await storage.load()
    except Exception as e:
        raise HTTPException(500, f'Could not read the sheet: {e}')
    # Recreate approval queues for any websites loaded from storage — they are
    # not persisted across sessions (same as lifespan boot).
    for wid in websites:
        website_queues.setdefault(wid, asyncio.Queue())
    return {'refreshed': True, 'backend': storage.backend,
            'events': len(events), 'websites': len(websites),
            'message': f'Refreshed from Google Sheets: {len(events)} events, {len(websites)} websites.'}


# ----------------------------- Events ---------------------------------------
# Per-event fields that default from Settings when creating an event, and can
# be overridden per event (in the event form and the event edit form). Scripts
# and the Eventbrite draft path read these off the Event object, so scripts use
# whatever was set on the individual event.
PER_EVENT_ORG_FIELDS = (
    'website_url', 'organizer', 'organizer_first_name', 'organizer_last_name',
    'organizer_phone_number', 'organizer_email_address',
)


@app.get('/api/events')
async def get_events():
    return [event_out(eid, ev) for eid, ev in events.items()]


@app.post('/api/events')
async def create_event(payload: dict):
    required = ('title', 'short_summary', 'long_summary', 'image', 'start_date', 'end_date')
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise HTTPException(400, f'missing fields: {", ".join(missing)}')
    event = Event(
        title=payload['title'],
        short_summary=payload['short_summary'],
        long_summary=payload['long_summary'],
        image=payload['image'],
        start_date=datetime.fromisoformat(payload['start_date']),
        end_date=datetime.fromisoformat(payload['end_date']),
        categories=list(settings.categories),
        venue=settings.venue,
        location=settings.location,
        open_to_public=settings.open_to_public,
        event_cost=settings.event_cost,
        # Per-event fields: use whatever was sent, falling back to defaults.
        website_url=payload.get('website_url') or settings.website_url,
        organizer=payload.get('organizer') or settings.organizer,
        organizer_first_name=payload.get('organizer_first_name')
            or settings.organizer_first_name,
        organizer_last_name=payload.get('organizer_last_name')
            or settings.organizer_last_name,
        organizer_phone_number=payload.get('organizer_phone_number')
            or settings.organizer_phone_number,
        organizer_email_address=payload.get('organizer_email_address')
            or settings.organizer_email_address,
    )
    eid = uuid4()
    events[eid] = event
    storage.mark_dirty()
    return event_out(eid, event)


def _event_has_active_runner(event_id: UUID) -> bool:
    """True if a one-off task using this event is still running."""
    for tid, t in tasks.items():
        if t.event_id == event_id and tid in runners and not runners[tid].done():
            return True
    return False


@app.put('/api/events/{event_id}')
async def update_event(event_id: UUID, payload: dict):
    """Edit the per-event fields of an existing event (title, summaries, image, dates).

    Settings-derived fields (venue, organizer, categories, ...) are left alone —
    they are not part of the event edit form.
    """
    if event_id not in events:
        raise HTTPException(404, 'Event not found')
    if _event_has_active_runner(event_id):
        raise HTTPException(
            409, 'a task is using this event — wait for it to finish (or cancel it) before editing')
    required = ('title', 'short_summary', 'long_summary', 'image', 'start_date', 'end_date')
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise HTTPException(400, f'missing fields: {", ".join(missing)}')
    try:
        start = datetime.fromisoformat(payload['start_date'])
        end = datetime.fromisoformat(payload['end_date'])
    except (TypeError, ValueError):
        raise HTTPException(400, 'invalid date format (use YYYY-MM-DDTHH:MM)')
    if end < start:
        raise HTTPException(400, 'end date must be on or after start date')
    ev = events[event_id]
    ev.title = payload['title']
    ev.short_summary = payload['short_summary']
    ev.long_summary = payload['long_summary']
    ev.image = payload['image']
    ev.start_date = start
    ev.end_date = end
    # Per-event overrides (optional: absent fields keep their current value;
    # blank means "use the Settings default", same as on create).
    for f in PER_EVENT_ORG_FIELDS:
        if f in payload:
            setattr(ev, f, payload[f] or getattr(settings, f))
    storage.mark_dirty()
    return event_out(event_id, ev)


@app.get('/api/events/{event_id}')
async def get_event(event_id: UUID):
    if event_id not in events:
        raise HTTPException(404, 'Event not found')
    return event_out(event_id, events[event_id])


@app.delete('/api/events/{event_id}')
async def delete_event(event_id: UUID):
    if event_id not in events:
        raise HTTPException(404, 'Event not found')
    del events[event_id]
    # Remove this event from any website's added_events log.
    for w in websites.values():
        if str(event_id) in w.added_events:
            del w.added_events[str(event_id)]
    storage.mark_dirty()
    return {'ok': True}


# ----------------------------- Tasks -----------------------------------------
@app.get('/api/events/{event_id}/tasks')
async def get_event_tasks(event_id: UUID):
    return [task_out(tid, t) for tid, t in tasks.items() if t.event_id == event_id]


@app.post('/api/events/{event_id}/tasks')
async def create_task(event_id: UUID, payload: dict):
    if event_id not in events:
        raise HTTPException(404, 'Event not found')
    url = (payload.get('url') or '').strip()
    if not url:
        raise HTTPException(400, 'url is required')
    task = Task(url=url, event_id=event_id)
    tasks[task.id] = task
    input_queues[task.id] = asyncio.Queue()
    runners[task.id] = asyncio.create_task(run_task(task.id))
    return task_out(task.id, task)


@app.get('/api/tasks/{task_id}')
async def get_task(task_id: UUID):
    if task_id not in tasks:
        raise HTTPException(404, 'Task not found')
    return task_out(task_id, tasks[task_id])


@app.post('/api/tasks/{task_id}/message')
async def send_message(task_id: UUID, payload: dict):
    if task_id not in tasks:
        raise HTTPException(404, 'Task not found')
    content = (payload.get('content') or '').strip()
    if not content:
        raise HTTPException(400, 'content is required')
    task = tasks[task_id]
    task.log.append({'role': 'user', 'content': content})
    await input_queues[task_id].put(content)
    return {'ok': True}


# ----------------------------- Websites --------------------------------------
@app.post('/api/websites')
async def create_website(payload: dict):
    kind = (payload.get('kind') or 'agentic').strip().lower()
    if kind not in ('agentic', 'eventbrite'):
        raise HTTPException(400, 'kind must be agentic or eventbrite')
    url = (payload.get('url') or '').strip()
    name = (payload.get('name') or '').strip() or url
    if kind == 'agentic' and not url:
        raise HTTPException(400, 'url is required for agentic websites')
    if kind == 'eventbrite':
        if any(w.kind == 'eventbrite' for w in websites.values()):
            raise HTTPException(409, 'only one Eventbrite website is supported')
        if not eventbrite_api.EVENTBRITE_TOKEN:
            raise HTTPException(400, 'EVENTBRITE_TOKEN env var is not set')
        if not url:
            # URL is just a label for eventbrite sites (the API base is hardcoded);
            # default to the Eventbrite homepage as a harmless placeholder.
            url = 'https://www.eventbrite.com/'
    w = Website(url=url, name=name, kind=kind)
    if kind == 'eventbrite':
        # No script/onboarding exists for Eventbrite sites, so 'unscripted' is
        # misleading. Use 'ready' to indicate it's a valid, configured site.
        w.status = 'ready'
    websites[w.id] = w
    website_queues[w.id] = asyncio.Queue()
    storage.mark_dirty()
    return website_out(w)


@app.put('/api/websites/{website_id}')
async def update_website(website_id: UUID, payload: dict):
    """Edit a website's name and/or URL.

    - `kind` is immutable (change only by delete + recreate).
    - Editing while onboarding is running is refused (409) — reloading the
      object mid-flight would desync the agent.
    - Changing the URL of a **scripted** site invalidates its generated script
      (the script calls `api.navigate` with the old URL), so the site is reset
      to 'unscripted' and the saved routine is cleared.
    """
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    if website_id in website_runners and not website_runners[website_id].done():
        raise HTTPException(
            409, 'setup is running for this website — wait for it to finish (or cancel it) before editing')

    url = (payload.get('url') or '').strip()
    name = (payload.get('name') or '').strip()
    if not url and not name:
        raise HTTPException(400, 'nothing to update')

    # URL changed: a scripted site's saved routine is now wrong.
    if url and url != w.url:
        if w.status == 'scripted':
            w.status = 'unscripted'
            w.phase = 'idle'
            w.scripts = []
            w.settings_version = ''
            # Close any review context left open for this website.
            ctx = review_contexts.pop(website_id, None)
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
            clear_live_page(website_id)
            w.log.append({'role': 'user',
                          'content': 'URL changed — saved routine cleared for this website; set it up again.'})
        w.url = url
    if name:
        w.name = name
    storage.mark_dirty()
    return website_out(w)


@app.get('/api/websites')
async def list_websites():
    return [website_out(w) for w in websites.values()]


@app.get('/api/websites/{website_id}')
async def get_website(website_id: UUID):
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    return website_out(w, include_log=True)


@app.delete('/api/websites/{website_id}')
async def delete_website(website_id: UUID):
    if website_id not in websites:
        raise HTTPException(404, 'Website not found')
    # Cancel any running onboarding, drop the queue, and clear reg entries.
    if website_id in website_runners:
        website_runners[website_id].cancel()
        del website_runners[website_id]
    website_queues.pop(website_id, None)
    # Close any review context left open for this website.
    ctx = review_contexts.pop(website_id, None)
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    clear_live_page(website_id)
    del websites[website_id]
    storage.mark_dirty()
    return {'ok': True}


@app.post('/api/websites/{website_id}/onboard')
async def onboard_website(website_id: UUID, payload: dict):
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    event_id = _parse_event_id(payload.get('event_id'))
    if event_id is None:
        raise HTTPException(400, 'valid event_id is required')
    if website_id in website_runners and not website_runners[website_id].done():
        # A live runner blocks re-onboarding — unless the site was already
        # cancelled/reset (stale runner still unwinding).
        if w.status == 'scripting':
            raise HTTPException(409, 'onboarding already in progress')
        else:
            website_runners.pop(website_id, None)
    # Ensure a queue exists (loaded-from-storage sites don't have one yet).
    website_queues.setdefault(website_id, asyncio.Queue())
    website_runners[website_id] = asyncio.create_task(run_onboarding(website_id, event_id))
    return website_out(w)


@app.post('/api/websites/{website_id}/respond')
async def respond_website(website_id: UUID, payload: dict):
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    action = (payload.get('action') or '').strip().lower()
    if action not in ('approve', 'reject'):
        raise HTTPException(400, 'action must be approve or reject')
    reason = (payload.get('reason') or '').strip()
    w.log.append({'role': 'user', 'content': f'[human] {action}' + (f': {reason}' if reason else '')})
    q = website_queues.setdefault(website_id, asyncio.Queue())
    await q.put({'action': action, 'feedback': reason})
    return {'ok': True}


@app.post('/api/websites/{website_id}/cancel')
async def cancel_onboarding(website_id: UUID):
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    runner = website_runners.get(website_id)
    if runner is not None and not runner.done():
        # Abort the underlying task (handles mid-load / mid-generation, which
        # the queue-based cancel alone cannot interrupt).
        runner.cancel()
        # Also drop a cancel on the queue in case the task is parked at
        # wait_action and absorbs the cancellation into a handled branch.
        if website_id in website_queues:
            await website_queues[website_id].put({'action': 'cancel', 'feedback': ''})
        # Best-effort wait for the task to unwind without cancelling this
        # endpoint (shield swallows the target's cancellation).
        try:
            await asyncio.wait_for(asyncio.shield(runner), timeout=2)
        except asyncio.CancelledError:
            pass  # shield surfaced the target's cancellation; expected
        except Exception:
            pass
        # Drop the runner reference so a subsequent onboard isn't blocked.
        website_runners.pop(website_id, None)
    # Reset the site synchronously regardless of which path above ran, so a
    # cancel during the loading/first-generation phase takes effect immediately.
    w.status = 'unscripted'
    w.phase = 'idle'
    w.scripts = []
    w.settings_version = ''
    w.log.append({'role': 'user',
                  'content': 'Onboarding cancelled — site reset.'})
    # Eagerly close the agent's browser session(s) so a stuck/in-flight agent
    # (blocked in a long LLM request) can't keep working or leave a window open.
    try:
        await manager.close_session()
    except Exception:
        pass
    clear_live_page(website_id)
    storage.mark_dirty()
    return {'ok': True}


@app.post('/api/websites/{website_id}/run')
async def run_website_script(website_id: UUID, payload: dict):
    """Run a scripted website's approved script against an event (per-run, human reviews)."""
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    if w.status != 'scripted':
        raise HTTPException(400, 'website is not scripted yet')
    if w.settings_version and w.settings_version != current_settings_version():
        raise HTTPException(409, 'settings changed; this script is stale, please re-onboard')
    scripts = [s for s in w.scripts if s.status == 'approved']
    if not scripts:
        raise HTTPException(400, 'no approved script for this website')
    event_id = _parse_event_id(payload.get('event_id'))
    if event_id is None:
        raise HTTPException(400, 'valid event_id is required')
    event = events[event_id]
    # Preflight the image URL (best-effort): an unshared Drive file serves HTML
    # instead of image bytes, which silently breaks the calendar site's own
    # image fetch during review/submit. Surface it in the response + site log.
    img_warn = ''
    if getattr(event, 'image', ''):
        try:
            from eventbrite_api import check_direct_image_url, direct_image_url
            img_url = direct_image_url(event.image)
            ok_img, why = await asyncio.to_thread(check_direct_image_url, img_url)
            if not ok_img:
                img_warn = (f'Image URL check failed: {why}. The calendar site may reject '
                            'the image. If it\'s a Google Drive link, share the file as '
                            '"Anyone with the link" (Viewer).')
                w.log.append({'role': 'user', 'content': f'[warn] {img_warn}'})
        except Exception:
            pass
    # Close any prior review context for this website so only one filled form
    # stays open at a time (prevents unbounded context leaks on repeated runs).
    prev = review_contexts.get(website_id)
    if prev is not None:
        try:
            await prev.close()
        except Exception:
            pass
    ok, msg, page, context = await run_script_against_event(
        scripts[0], event, live_key=str(website_id))
    if not ok:
        raise HTTPException(500, msg)
    # Keep the filled page OPEN so the human can review and submit it manually
    # (either in the visible browser window or the embedded view). Save the
    # context so we can close it when the site is run again (or the app shuts down).
    if context is not None:
        review_contexts[website_id] = context
    msg = ('Script executed. Review and submit the form in the browser \
or the embedded view, then click Done.')
    if img_warn:
        msg = '⚠️ ' + img_warn + '\n\n' + msg
    return {'ok': True, 'message': msg}


@app.post('/api/websites/{website_id}/done')
async def finish_website_review(website_id: UUID):
    """Close the review page for a scripted run once the human has submitted.

    The embedded browser view streams the review page; this endpoint closes it
    (like "close the tab") and clears the live-page pointer.
    """
    ctx = review_contexts.pop(website_id, None)
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    clear_live_page(website_id)
    return {'ok': True}


@app.post('/api/websites/{website_id}/configure-eventbrite')
async def configure_eventbrite(website_id: UUID, payload: dict):
    """Save venue/organizer ids for an Eventbrite website (reused for every add)."""
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    if w.kind != 'eventbrite':
        raise HTTPException(400, 'website is not an Eventbrite site')
    w.venue_id = (payload.get('venue_id') or '').strip()
    w.organizer_id = (payload.get('organizer_id') or '').strip()
    w.log.append({'role': 'user', 'content':
                  f'[config] Eventbrite venue={w.venue_id} organizer={w.organizer_id}'})
    storage.mark_dirty()
    return website_out(w)


@app.post('/api/websites/{website_id}/add-event')
async def add_event_eventbrite(website_id: UUID, payload: dict):
    """Create a DRAFT Eventbrite event from an Event. Publishing stays manual."""
    w = websites.get(website_id)
    if not w:
        raise HTTPException(404, 'Website not found')
    if w.kind != 'eventbrite':
        raise HTTPException(400, 'website is not an Eventbrite site')
    if not w.venue_id or not w.organizer_id:
        raise HTTPException(400, 'Eventbrite venue_id/organizer_id not configured yet')
    if not eventbrite_api.EVENTBRITE_TOKEN:
        raise HTTPException(500, 'EVENTBRITE_TOKEN env var is not set')
    event_id = _parse_event_id(payload.get('event_id'))
    if event_id is None or event_id not in events:
        raise HTTPException(400, 'valid event_id is required')
    if str(event_id) in w.added_events:
        raise HTTPException(409, f'already added: {w.added_events[str(event_id)]}')
    event = events[event_id]

    # Attach the website's venue/organizer ids for the API call.
    event._eventbrite_venue_id = w.venue_id
    event._eventbrite_organizer_id = w.organizer_id

    # Optional image upload (best-effort): Drive links converted to direct URLs.
    # All of these are sync requests runs on a thread so they don't block the
    # event loop (small VM — one stalled HTTP call would freeze every request).
    logo_id = None
    if event.image:
        data, ctype = await asyncio.to_thread(eventbrite_api.fetch_image_bytes, event.image)
        if data:
            logo_id = await asyncio.to_thread(eventbrite_api.upload_image, data, ctype)
        if not logo_id:
            w.log.append({'role': 'user', 'content':
                         '[warn] could not upload image to Eventbrite — event created without an image'})

    try:
        resp = await asyncio.to_thread(eventbrite_api.create_event, event, logo_id=logo_id)
        eb_id = resp.get('id')
        await asyncio.to_thread(eventbrite_api.add_free_ticket_class, str(eb_id))
    except Exception as e:
        w.log.append({'role': 'user', 'content': f'[error] Eventbrite create failed: {e}'})
        storage.mark_dirty()
        raise HTTPException(502, str(e))

    url = f'https://www.eventbrite.com/e/{eb_id}'
    w.added_events[str(event_id)] = url
    w.log.append({'role': 'user', 'content': f'[eventbrite] draft created: {url}'})
    storage.mark_dirty()
    return {'ok': True, 'eventbrite_url': url,
            'message': 'Draft created on Eventbrite. Review and publish it there manually.'}


# ------------------------- Embedded browser view -----------------------------
# Streams a live screenshot of the website's open browser page over WebSocket
# (rate/quality configurable via state.BROWSER_FPS / state.BROWSER_JPEG_QUALITY),
# and proxies the human's edits/clicks/keys back into the real Playwright page —
# but only while the human is expected to interact (approval phases, or a
# scripted run's review page). Otherwise it is view-only.

HUMAN_INTERACT_PHASES = {'manual_approval', 'script_approval'}


def _page_interactive(w: Website | None) -> bool:
    """Whether the human may edit the live page right now."""
    if w is None:
        return False
    if w.phase in HUMAN_INTERACT_PHASES:
        return True
    # A scripted run's review page stays open for manual editing/submission.
    if w.status == 'scripted' and get_live_page(w.id) is not None:
        return True
    return False


async def _resolve_live_page(website_id: UUID):
    """Return the page the embedded view should stream for this website, or None.

    Order: explicitly registered review pages (scripted runs / onboarding's
    script run), then the agent's own session page while it fills manually.
    """
    page = get_live_page(website_id)
    if page is not None:
        return page
    w = websites.get(website_id)
    if w is not None and w.kind != 'eventbrite' \
            and w.status == 'scripting' and manager.latest_page is not None:
        # The agent's own browser session while onboarding runs.
        return manager.latest_page
    return None


async def _handle_view_event(website_id: UUID, raw: str):
    """Apply a click/type/key/scroll from the embedded view to the live page."""
    try:
        ev = json.loads(raw)
    except Exception:
        return
    if not isinstance(ev, dict):
        return
    w = websites.get(website_id)
    kind = ev.get('type')
    # Scrolls are fine while the agent works (view-only panning); other input
    # (click/type/key) still requires an interactive phase.
    if kind != 'scroll' and not _page_interactive(w):
        return
    page = await _resolve_live_page(website_id)
    if page is None:
        return
    try:
        if kind == 'click':
            await page.mouse.click(float(ev.get('x', 0)), float(ev.get('y', 0)))
        elif kind == 'dblclick':
            await page.mouse.dblclick(float(ev.get('x', 0)), float(ev.get('y', 0)))
        elif kind == 'type':
            await page.keyboard.type(str(ev.get('text', '')))
        elif kind == 'paste':
            # The human's OS clipboard text, sent from the embedded view. insert_text
            # is the paste primitive: it fires input events (so autosuggest works)
            # and inserts into the currently-focused element without per-key delay.
            text = str(ev.get('text') or '')[:10000]
            if text:
                await page.keyboard.insert_text(text)
        elif kind == 'key':
            await page.keyboard.press(str(ev.get('key', '')))
        elif kind == 'scroll':
            # Move the pointer where the user is looking, then scroll NATIVELY:
            # mouse.wheel dispatches a real wheel event at the pointer, so the
            # browser scrolls whatever is under it — including content inside
            # iframes, which a JS elementFromPoint() walk can't see. Deltas are
            # already pixel-normalized client-side (~120px/notch).
            x = float(ev.get('x') or 0)
            y = float(ev.get('y') or 0)
            dx = int(ev.get('dx', 0))
            dy = int(ev.get('dy', 0))
            if not (dx or dy):
                return
            try:
                await page.mouse.move(x, y)
            except Exception:
                pass
            try:
                await page.mouse.wheel(dx, dy)
            except Exception:
                # Fallback: scroll the nearest scrollable ancestor of the
                # element under the pointer (works when wheel() is unavailable).
                try:
                    await page.evaluate(
                        """(px, py, dx, dy) => {
                            const findTarget = () => {
                                let el = null;
                                try { el = document.elementFromPoint(px, py); } catch (_) { el = null; }
                                while (el) {
                                    const s = getComputedStyle(el);
                                    const canY = /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight;
                                    const canX = /(auto|scroll)/.test(s.overflowX) && el.scrollWidth > el.clientWidth;
                                    if (canX || canY) return el;
                                    el = el.parentElement;
                                }
                                return document.scrollingElement || document.documentElement;
                            };
                            const t = findTarget();
                            if (dx && t.scrollWidth > t.clientWidth) t.scrollLeft += dx;
                            if (dy && t.scrollHeight > t.clientHeight) t.scrollTop += dy;
                        }""", x, y, dx, dy)
                except Exception:
                    pass
    except Exception:
        pass  # page closed mid-click; the stream shows 'empty' next tick


async def _start_screencast(page):
    """Start Chromium's CDP screencast pipeline for a page.

    Returns (cdp_session, frames_queue) on success, or None when CDP isn't
    available (Firefox/WebKit) so the caller falls back to page.screenshot().
    The queue holds the single most recent frame dict ({"data": base64 jpeg,
    "sessionId": ..., "metadata": {...}}) — each received frame is acked
    immediately (Chromium stops streaming otherwise) and the loop sends the
    latest one per its fps ceiling, dropping in-between frames for free.
    """
    try:
        session = await page.context.new_cdp_session(page)
    except Exception:
        return None  # CDP sessions are Chromium-only
    frames: asyncio.Queue = asyncio.Queue(maxsize=1)

    def on_frame(params: dict):
        # Chromium requires every screencast frame to be acknowledged or it
        # stops emitting new ones.
        sid = (params or {}).get('sessionId')
        if sid:
            try:
                asyncio.get_running_loop().create_task(
                    session.send('Page.screencastFrameAck', {'sessionId': sid}))
            except Exception:
                pass
        try:
            if frames.full():
                frames.get_nowait()  # drop stale frame, keep the freshest
            frames.put_nowait(params)
        except Exception:
            pass

    session.on('Page.screencastFrame', on_frame)

    def on_close(*_a):
        # Page/context died: let the main loop see an empty queue so it
        # re-attaches (or falls back) next tick instead of sending dead frames.
        try:
            while not frames.empty():
                frames.get_nowait()
        except Exception:
            pass

    session.on('close', on_close)
    try:
        vp = page.viewport_size or {'width': 1280, 'height': 850}
        await session.send('Page.startScreencast', {
            'format': 'jpeg',
            'quality': BROWSER_JPEG_QUALITY,
            'everyNthFrame': 1,
            'maxWidth': vp.get('width'),
            'maxHeight': vp.get('height'),
        })
    except Exception:
        try:
            await session.detach()
        except Exception:
            pass
        return None
    return session, frames


async def _stop_screencast(cdp):
    """Idempotently stop + detach a CDP screencast session (or None)."""
    if not cdp:
        return
    session, _frames = cdp
    try:
        await session.send('Page.stopScreencast')
    except Exception:
        pass
    try:
        await session.detach()
    except Exception:
        pass


def _log_sent_fps(wid, frames_sent, fps_logged_at):
    """Count a sent frame; log achieved fps every ~2s. Returns (frames_sent, fps_logged_at)."""
    frames_sent += 1
    now = asyncio.get_running_loop().time()
    if now - fps_logged_at >= 2.0:
        fps = frames_sent / max(now - fps_logged_at, 1e-6)
        print(f'[browser-ws] {wid} ~{fps:.1f}fps sent (BROWSER_FPS={BROWSER_FPS:g})')
        frames_sent = 0
        fps_logged_at = now
    return frames_sent, fps_logged_at


@app.websocket('/api/browser/{website_id}/ws')
async def browser_stream(websocket: WebSocket, website_id: str):
    """Stream the live page as JPEG frames (state.BROWSER_FPS / QUALITY); accept human edit events.

    Uses Chromium's CDP screencast pipeline when the browser is Chromium (high
    fps at low CPU — no per-frame re-capture), and falls back to
    page.screenshot() for Firefox/WebKit.
    """
    try:
        wid = UUID(website_id)
    except ValueError:
        print(f'[browser-ws] rejected malformed id: {website_id!r}')
        await websocket.close(code=1008)
        return
    print(f'[browser-ws] websocket connected for {wid} (from {websocket.client.host})')
    await websocket.accept()
    last_frame: bytes | None = None
    next_frame_at = 0.0
    frame_interval = 1.0 / BROWSER_FPS
    # The event poll must never gate frame production: keep it a fraction of the
    # frame budget so a high BROWSER_FPS isn't capped by the 50ms receive wait.
    event_poll = min(0.05, frame_interval / 2)
    frames_sent = 0
    fps_logged_at = 0.0
    cdp = None            # (session, frames_queue) when streaming via CDP
    cdp_page = None       # page identity the CDP session is attached to
    try:
        while True:
            # Process a BOUNDED batch of human events so a flood (continuous
            # scroll/typing) can't starve frame production — events interleave
            # with frames instead of blocking them entirely (the old unbounded
            # drain + `drained: continue` froze the view until input stopped).
            _handled, clicked = await _drain_view_events(
                websocket, wid, timeout=event_poll, max_events=16)
            if clicked:
                # Make menu clicks visible immediately: skip the fps gate this
                # iteration so the freshest frame is sent right away.
                next_frame_at = 0.0
                try:
                    await asyncio.wait_for(_hint_native_control(wid, websocket), 5.0)
                except Exception:
                    pass
            tick = asyncio.get_running_loop().time()
            if tick < next_frame_at:
                # Small bounded sleep: keeps the frame clock honest while still
                # polling events promptly (so the post-click frame appears fast).
                await asyncio.sleep(min(0.02, next_frame_at - tick))
                continue
            next_frame_at = tick + frame_interval
            try:
                w = websites.get(wid)
                page = await _resolve_live_page(wid)
                if page is None:
                    if cdp is not None:
                        await _stop_screencast(cdp)
                        cdp, cdp_page = None, None
                    if not await _safe_send(websocket, {'type': 'empty', 'interactive': False,
                                                        'phase': w.phase if w else ''}):
                        break
                    last_frame = None
                    continue
                # (Re)attach the CDP screencast when the visible page changes.
                if cdp_page is not page:
                    if cdp is not None:
                        try:
                            await _stop_screencast(cdp)
                        except Exception:
                            pass
                    try:
                        cdp = await asyncio.wait_for(_start_screencast(page), 5.0)
                    except Exception:
                        cdp = None
                    cdp_page = page  # remember even if None (avoid retry every tick)
                if cdp is not None:
                    # CDP screencast: send the freshest frame if we have one.
                    params = None
                    try:
                        params = cdp[1].get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    if params is not None:
                        data_b64 = (params.get('data') or '')
                        meta = params.get('metadata') or {}
                        vs = meta.get('visibleSize') or {}
                        try:
                            vp = page.viewport_size or {}
                        except Exception:
                            vp = {}
                        vw = vs.get('width') or vp.get('width') or 0
                        vh = vs.get('height') or vp.get('height') or 0
                        if data_b64 and data_b64 != last_frame:
                            if not await _safe_send(websocket, {
                                    'type': 'frame',
                                    'data': data_b64,
                                    'vw': vw, 'vh': vh,
                                    'interactive': _page_interactive(w),
                                    'phase': w.phase if w else ''}):
                                break
                            last_frame = data_b64
                            frames_sent, fps_logged_at = _log_sent_fps(
                                wid, frames_sent, fps_logged_at)
                            continue
                    # No new/unchanged frame yet → lightweight heartbeat.
                    if not await _safe_send(websocket, {'type': 'ping',
                                                        'interactive': _page_interactive(w),
                                                        'phase': w.phase if w else ''}):
                        break
                    continue
                try:
                    # Bound the capture: a hung page/browser must not wedge the
                    # stream loop (that froze the view and dropped input).
                    frame = await asyncio.wait_for(
                        page.screenshot(type='jpeg', quality=BROWSER_JPEG_QUALITY), 3.0)
                    vw, vh = page.viewport_size or (0, 0)
                except Exception:
                    # Page closed under us (e.g. a run was finished/cancelled).
                    clear_live_page(wid)
                    if not await _safe_send(websocket, {'type': 'empty', 'interactive': False,
                                                        'phase': w.phase if w else ''}):
                        break
                    last_frame = None
                    continue
                if frame != last_frame:
                    if not await _safe_send(websocket, {
                            'type': 'frame',
                            'data': b64encode(frame).decode('ascii'),
                            'vw': vw, 'vh': vh,
                            'interactive': _page_interactive(w),
                            'phase': w.phase if w else ''}):
                        break
                    last_frame = frame
                    frames_sent, fps_logged_at = _log_sent_fps(
                        wid, frames_sent, fps_logged_at)
                else:
                    # Lightweight heartbeat so the client knows the stream is live.
                    if not await _safe_send(websocket, {'type': 'ping',
                                                        'interactive': _page_interactive(w),
                                                        'phase': w.phase if w else ''}):
                        break
            except WebSocketDisconnect:
                raise
            except Exception:
                # Transient page/CDP hiccup (e.g. the agent's page closed mid-
                # fill on some sites): degrade to 'empty' and keep the stream
                # alive instead of letting the error kill the whole handler
                # (which froze the view and let wheels hit the app page).
                if cdp is not None:
                    try:
                        await _stop_screencast(cdp)
                    except Exception:
                        pass
                    cdp, cdp_page = None, None
                clear_live_page(wid)
                if not await _safe_send(websocket, {'type': 'empty', 'interactive': False,
                                                    'phase': (websites.get(wid).phase
                                                              if wid in websites else '')}):
                    break
                last_frame = None
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if cdp is not None:
            try:
                await _stop_screencast(cdp)
            except Exception:
                pass
            cdp, cdp_page = None, None


async def _safe_send(websocket: WebSocket, obj: dict, timeout: float = 5.0) -> bool:
    """Send a WS message without letting backpressure wedge the stream loop.

    Returns False if the client is gone, refusing capacity, or the send didn't
    complete in time (slow tunnel / backgrounded tab). The caller breaks out of
    the loop instead of letting send_json() await forever while events pile up.
    """
    try:
        async with asyncio.timeout(timeout):
            await websocket.send_json(obj)
        return True
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        return False


async def _drain_view_events(websocket: WebSocket, wid: UUID, timeout: float = 0.05,
                             max_events: int = 0) -> tuple[bool, bool]:
    """Handle queued human events; return (handled_any, handled_click).

    Processes at most `max_events` events (0 = unlimited) so a flood of scroll
    events can't monopolize the loop and starve frame production. Clicks are
    flagged so the caller can flush a frame immediately after them.
    """
    saw = False
    saw_click = False
    count = 0
    while True:
        if max_events and count >= max_events:
            break
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
            if isinstance(raw, str):
                try:
                    ev = json.loads(raw)
                    if isinstance(ev, dict) and ev.get('type') == 'click':
                        saw_click = True
                except Exception:
                    pass
            # Bound the replay of a single event too: a hung page must not block
            # the drain (and thus the whole stream) waiting on a click/type that
            # will never complete.
            try:
                await asyncio.wait_for(_handle_view_event(wid, raw), 5.0)
            except Exception:
                pass
            saw = True
            count += 1
        except asyncio.TimeoutError:
            break
        except (WebSocketDisconnect, RuntimeError):
            raise
    return saw, saw_click


async def _hint_native_control(wid: UUID, websocket: WebSocket):
    """Tell the client when the focused element is a native control whose popup
    can't appear in the stream.

    Native Chromium date pickers and <select> dropdowns are rendered as browser
    chrome popups, NOT page content — CDP screencast (and page.screenshot) can't
    capture them. After a click lands on one, push a hint so the human knows to
    type/use the keyboard instead of waiting for a popup that will never show.
    Best-effort; never raises.
    """
    try:
        page = await _resolve_live_page(wid)
        if page is None:
            return
        info = await page.evaluate("""() => {
            const el = document.activeElement;
            if (!el) return null;
            const tag = (el.tagName || '').toLowerCase();
            const type = el.type ? String(el.type).toLowerCase() : '';
            if (tag === 'select') return 'select';
            if (tag === 'input' && type === 'date') return 'date';
            return null;
        }""")
        if info == 'select':
            await websocket.send_json({'type': 'hint',
                'text': "Native dropdown\u2019t render in the live view — use ArrowDown/Up + Enter to pick an option."})
        elif info == 'date':
            await websocket.send_json({'type': 'hint',
                'text': "Native date picker doesn\u2019t show in the live view — click the field and type the date (e.g. 12/25/2026), then Enter."})
    except Exception:
        pass


# Static assets (style.css, app.js) are served from /static so they can
# carry a cache-busting version query generated at request time.
app.mount('/static', StaticFiles(directory=str(FRONTEND)), name='static')
