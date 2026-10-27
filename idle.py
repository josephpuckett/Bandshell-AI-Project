"""Idle shutdown — power the host off when nobody is using the app.

Why: Bandshell Calendar is built for occasional use (a handful of events a month),
but a running VM bills by the hour whether or not anyone is watching. The watchdog
powers the machine off after `AUTO_SHUTDOWN_MINUTES` of *real* inactivity, so cost
tracks use. `AUTO_SHUTDOWN_MINUTES=0` (the default) disables it entirely, so a dev
machine can never power itself off.

What counts as activity
-----------------------
* any HTTP request **except** the endpoints the UI polls on a timer
  (`GET /api/websites/{id}`), plus `/api/health` and `/api/activity`. This is the
  whole point: a forgotten browser tab polls forever, and that must not keep the
  machine awake. Everything else — loading the page, creating an event, pressing
  Approve — is a human.
* any inbound live-view WebSocket message (click / typing / scroll / paste) and
  every new WebSocket connection.
* process start.

What blocks a shutdown
----------------------
`busy_check()` is supplied by `main.py` and reports work in flight: agent/script
runner tasks that haven't finished, a website waiting for human approval, and open
*review contexts* (a scripted run's filled form that the human hasn't submitted
yet). Powers off would lose that work, so the watchdog waits for it.
`AUTO_SHUTDOWN_MAX_HOURS` is the escape hatch for a genuinely abandoned review page:
after that much uptime it shuts down regardless (0 = never force).

Safety
------
* the command is `AUTO_SHUTDOWN_COMMAND` (default
  `sudo -n /usr/sbin/shutdown -h now`) — one sudoers line, written by
  `deploy/install.sh`. `-n` matters: a password prompt would hang the watchdog.
* SIGTERM from systemd then triggers uvicorn's graceful shutdown, whose lifespan
  hook cancels runners, closes review contexts and flushes storage (Google Sheets)
  before exit.
* `AUTO_SHUTDOWN_DRY_RUN=1` logs the decision and skips the command — use it to
  watch the timer on a real host without risking a surprise power-off.
* after a failed command there is a 5-minute cooldown, so a broken sudoers file
  doesn't fill the journal with an attempt every tick.
"""

import asyncio
import logging
import re
import shlex
import time
from typing import Awaitable, Callable

import state

log = logging.getLogger('bandshell.idle')

# Endpoints that must NOT count as human activity:
#  - the live-view/website panel's 1s poll (a forgotten tab would otherwise keep
#    the host up forever),
#  - health/activity probes (uptime monitors, the installer's checks).
_EXEMPT_PATHS = {'/api/health', '/api/activity'}
_POLL_PATH_RE = re.compile(r'^/api/websites/[^/]+$')

_RETRY_COOLDOWN = 300.0     # seconds after a shutdown attempt (success or failure)

_started = time.monotonic()
_last_human = time.monotonic()
_open_streams = 0
_human_events = 0
_shutdown_attempts = 0
_last_attempt = 0.0
_shutdown_reason = ''
_inflight = False
_done = False               # the command was accepted; stop deciding
_task: asyncio.Task | None = None
_watchdog: Callable[[], Awaitable[None]] | None = None
_busy_check: Callable[[], bool] | None = None
_before_shutdown: Callable[[str], Awaitable[None]] | None = None


# --------------------------------------------------------------------------- #
# Recording activity
# --------------------------------------------------------------------------- #
def note_human(events: int = 1) -> None:
    """Record genuine human activity (resets the idle timer)."""
    global _last_human, _human_events
    _last_human = time.monotonic()
    _human_events += events


def note_request(path: str, method: str) -> None:
    """Record an HTTP request, ignoring the UI's own polling."""
    if method.upper() == 'GET':
        if path in _EXEMPT_PATHS or _POLL_PATH_RE.match(path):
            return
    note_human()


def stream_open() -> None:
    global _open_streams
    _open_streams += 1
    note_human()


def stream_close() -> None:
    global _open_streams
    _open_streams = max(0, _open_streams - 1)
    note_human()


# --------------------------------------------------------------------------- #
# State snapshot (GET /api/activity)
# --------------------------------------------------------------------------- #
def _minutes() -> float:
    return float(state.AUTO_SHUTDOWN_MINUTES or 0)


def _max_hours() -> float:
    return float(state.AUTO_SHUTDOWN_MAX_HOURS or 0)


def _idle_seconds() -> float:
    return time.monotonic() - _last_human


def _uptime_seconds() -> float:
    return time.monotonic() - _started


def snapshot() -> dict:
    """Everything the /api/activity endpoint reports (and the watchdog decides on)."""
    busy = bool(_busy_check and _busy_check())
    idle = _idle_seconds()
    minutes = _minutes()
    max_hours = _max_hours()
    enabled = minutes > 0 or max_hours > 0
    remaining: float | None = None
    if enabled:
        if minutes > 0:
            remaining = max(0.0, minutes * 60.0 - idle)
        if max_hours > 0:
            forced = max(0.0, max_hours * 3600.0 - _uptime_seconds())
            remaining = forced if remaining is None else min(remaining, forced)
    return {
        'enabled': enabled,
        'idle_seconds': round(idle, 1),
        'uptime_seconds': round(_uptime_seconds(), 1),
        'open_streams': _open_streams,
        'human_events': _human_events,
        'busy': busy,
        'would_shut_down_in_seconds': None if (busy or not enabled) else (
            None if remaining is None else round(remaining, 1)),
        'auto_shutdown_minutes': minutes,
        'auto_shutdown_max_hours': max_hours,
        'dry_run': bool(state.AUTO_SHUTDOWN_DRY_RUN),
        'shutdown_attempts': _shutdown_attempts,
        'last_shutdown_reason': _shutdown_reason,
        'command': state.AUTO_SHUTDOWN_COMMAND,
    }


# --------------------------------------------------------------------------- #
# The watchdog
# --------------------------------------------------------------------------- #
def start(busy_check: Callable[[], bool] | None = None,
          before_shutdown: Callable[[str], Awaitable[None]] | None = None) -> None:
    """Start the watchdog (no-op when the feature is disabled)."""
    global _task, _busy_check, _before_shutdown, _watchdog
    _busy_check = busy_check
    _before_shutdown = before_shutdown
    if not (_minutes() > 0 or _max_hours() > 0):
        log.info('idle shutdown: disabled (AUTO_SHUTDOWN_MINUTES=0)')
        return
    _watchdog = _run
    _task = asyncio.create_task(_run(), name='idle-watchdog')
    log.info('idle shutdown: enabled — %s min idle, max %s h uptime, check every %ss%s',
             _minutes(), _max_hours() or '∞', state.AUTO_SHUTDOWN_CHECK_SECONDS,
             ' (DRY RUN)' if state.AUTO_SHUTDOWN_DRY_RUN else '')


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None


async def _run() -> None:
    interval = max(0.1, float(state.AUTO_SHUTDOWN_CHECK_SECONDS or 30))
    while True:
        await asyncio.sleep(interval)
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('idle shutdown: watchdog tick failed')


async def tick() -> None:
    """One decision round. Split out so tests can drive it without sleeping."""
    if _inflight or _done:
        return
    minutes, max_hours = _minutes(), _max_hours()
    if minutes <= 0 and max_hours <= 0:
        return
    busy = bool(_busy_check and _busy_check())
    idle = _idle_seconds()
    uptime = _uptime_seconds()
    forced = max_hours > 0 and uptime >= max_hours * 3600.0
    idle_due = minutes > 0 and idle >= minutes * 60.0
    if not (forced or (idle_due and not busy)):
        return
    # One attempt, then silence: after a SUCCESS the process is about to die, and
    # after a failure (no sudoers line, dry run) retrying every tick would spam
    # the journal. This applies to forced shutdowns too.
    if _last_attempt and time.monotonic() - _last_attempt < _RETRY_COOLDOWN:
        return
    if forced:
        reason = (f'max uptime {max_hours:g}h reached '
                  f'(idle {idle / 60:.0f} min, busy={busy})')
    else:
        reason = f'idle for {idle / 60:.1f} min (limit {minutes:g} min)'
    await request_shutdown(reason)


async def request_shutdown(reason: str) -> None:
    """Run the shutdown sequence once (the before_shutdown hook + the command)."""
    global _inflight, _shutdown_attempts, _last_attempt, _shutdown_reason
    if _inflight:
        return
    _inflight = True
    _shutdown_attempts += 1
    _last_attempt = time.monotonic()
    _shutdown_reason = reason
    try:
        log.warning('idle shutdown: %s', reason)
        if _before_shutdown is not None:
            try:
                await _before_shutdown(reason)
            except Exception:
                log.exception('idle shutdown: pre-shutdown hook failed (continuing)')
        if state.AUTO_SHUTDOWN_DRY_RUN:
            log.warning('idle shutdown: DRY RUN — would run: %s',
                        state.AUTO_SHUTDOWN_COMMAND)
            return
        await _run_command()
    finally:
        _inflight = False


async def _run_command() -> None:
    global _done
    argv = shlex.split(state.AUTO_SHUTDOWN_COMMAND or '')
    if not argv:
        log.error('idle shutdown: AUTO_SHUTDOWN_COMMAND is empty')
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        log.error('idle shutdown: command not found: %s', argv[0])
        return
    except Exception:
        log.exception('idle shutdown: could not start %s', argv[0])
        return
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        log.error('idle shutdown: %s did not finish in 30s', argv[0])
        return
    rc = proc.returncode
    if rc != 0:
        # Typical cause: no sudoers line for shutdown (or not running as the app
        # user). Loud, because the feature silently doesn't work otherwise.
        log.error('idle shutdown: %s exited %s — check the sudoers rule '
                  '(deploy/install.sh writes /etc/sudoers.d/bandshell-shutdown): %s',
                  ' '.join(argv), rc,
                  (err or out or b'').decode('utf-8', 'replace').strip()[:500])
    else:
        _done = True
        log.warning('idle shutdown: command accepted; the process should exit now')
