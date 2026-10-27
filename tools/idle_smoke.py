"""Smoke test for the idle-shutdown watchdog (`idle.py`).

    .venv/bin/python3 tools/idle_smoke.py

Needs no browser, no FastAPI app and no network: it drives `idle.py` directly.
That module holds mutable module-level state (last-activity timestamp, attempt
counter, cooldown), so every scenario runs in its OWN subprocess with its own
`AUTO_SHUTDOWN_*` environment — exactly how the service picks the values up at
import time.

Checks:
  1. disabled by default (AUTO_SHUTDOWN_MINUTES=0 → no task, enabled=False)
  2. fires after the idle limit (dry run: logs the decision, runs nothing)
  3. the UI's 1s poll does NOT count as activity; a real request DOES
  4. in-flight work ("busy") blocks a shutdown past the limit
  5. the command path runs and reports success (rc 0)
  6. a failing command logs once and then backs off (no attempt storm)
  7. AUTO_SHUTDOWN_MAX_HOURS forces a shutdown even while busy

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# Helpers shared by the scenarios (each runs in its own process)
# --------------------------------------------------------------------------- #
def _report(**kw) -> None:
    print('RESULT ' + json.dumps(kw))


def _task_running() -> bool:
    import idle

    task = getattr(idle, '_task', None)
    return bool(task is not None and not task.done())


def _read_marker() -> str:
    p = Path(os.environ['MARKER_FILE'])
    return p.read_text().strip() if p.exists() else 'missing'


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenario_disabled() -> int:
    import asyncio

    import idle
    import state

    assert state.AUTO_SHUTDOWN_MINUTES == 0, 'env not applied'

    async def _go() -> int:
        idle.start()
        snap = idle.snapshot()
        running = _task_running()
        await idle.stop()
        _report(enabled=snap['enabled'], watchdog_task=running)
        return 0 if (snap['enabled'] is False and not running) else 1

    return asyncio.run(_go())


def scenario_fires() -> int:
    import asyncio

    import idle

    async def _go() -> int:
        idle.start()
        await asyncio.sleep(0.9)          # limit 0.005 min = 0.3s, check every 0.1s
        snap = idle.snapshot()
        await idle.stop()
        _report(attempts=snap['shutdown_attempts'], reason=snap['last_shutdown_reason'])
        return 0 if snap['shutdown_attempts'] == 1 else 1

    return asyncio.run(_go())


def scenario_poll_exempt() -> int:
    import asyncio

    import idle

    async def _go() -> int:
        idle.start()
        await asyncio.sleep(0.3)
        before = idle.snapshot()['idle_seconds']
        # The website panel's 1s poll must not count…
        for _ in range(5):
            idle.note_request('/api/websites/2f3a1c00-0000-0000-0000-000000000000', 'GET')
            idle.note_request('/api/health', 'GET')
            await asyncio.sleep(0.05)
        after_polls = idle.snapshot()['idle_seconds']
        # …but a real request must.
        idle.note_request('/api/events', 'POST')
        after_human = idle.snapshot()['idle_seconds']
        await idle.stop()
        _report(before=before, after_polls=after_polls, after_human=after_human)
        grew = after_polls > before + 0.2      # idle kept growing through the polls
        reset = after_human < 0.1              # the POST reset the timer
        return 0 if (grew and reset) else 1

    return asyncio.run(_go())


def scenario_busy_blocks() -> int:
    import asyncio

    import idle

    async def _go() -> int:
        idle.start(busy_check=lambda: True)
        await asyncio.sleep(0.9)
        snap = idle.snapshot()
        await idle.stop()
        _report(attempts=snap['shutdown_attempts'], busy=snap['busy'],
                would_shut_down_in=snap['would_shut_down_in_seconds'])
        ok = snap['shutdown_attempts'] == 0 and snap['busy'] is True
        return 0 if ok else 1

    return asyncio.run(_go())


def scenario_command_ok() -> int:
    import asyncio

    import idle

    async def _go() -> int:
        await idle.request_shutdown('test: command path')
        marker = _read_marker()
        _report(marker=marker, attempts=idle.snapshot()['shutdown_attempts'])
        return 0 if marker == 'ran' else 1

    return asyncio.run(_go())


def scenario_command_fails() -> int:
    import asyncio

    import idle

    async def _go() -> int:
        # Idle limit 0.005 min = 0.3s; the command exits 1 on every tick.
        idle.start()
        await asyncio.sleep(1.2)
        snap = idle.snapshot()
        await idle.stop()
        _report(attempts=snap['shutdown_attempts'], reason=snap['last_shutdown_reason'])
        # One attempt, then the 5-minute cooldown suppresses the rest.
        return 0 if snap['shutdown_attempts'] == 1 else 1

    return asyncio.run(_go())


def scenario_force_max_hours() -> int:
    import asyncio

    import idle

    async def _go() -> int:
        # Busy forever, but MAX_HOURS is tiny → it must power off anyway.
        idle.start(busy_check=lambda: True)
        await asyncio.sleep(1.0)
        snap = idle.snapshot()
        await idle.stop()
        _report(attempts=snap['shutdown_attempts'], reason=snap['last_shutdown_reason'])
        return 0 if snap['shutdown_attempts'] == 1 else 1

    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
SCENARIOS: list[tuple[str, dict]] = [
    ('disabled by default (AUTO_SHUTDOWN_MINUTES=0)', dict(
        fn=scenario_disabled, env={'AUTO_SHUTDOWN_MINUTES': '0'})),
    ('fires after the idle limit (dry run)', dict(
        fn=scenario_fires, env={'AUTO_SHUTDOWN_MINUTES': '0.005',
                                'AUTO_SHUTDOWN_CHECK_SECONDS': '0.1',
                                'AUTO_SHUTDOWN_DRY_RUN': '1'})),
    ("UI polls don't count; real requests do", dict(
        fn=scenario_poll_exempt, env={'AUTO_SHUTDOWN_MINUTES': '60',
                                      'AUTO_SHUTDOWN_CHECK_SECONDS': '0.1',
                                      'AUTO_SHUTDOWN_DRY_RUN': '1'})),
    ('in-flight work blocks shutdown', dict(
        fn=scenario_busy_blocks, env={'AUTO_SHUTDOWN_MINUTES': '0.005',
                                      'AUTO_SHUTDOWN_CHECK_SECONDS': '0.1',
                                      'AUTO_SHUTDOWN_DRY_RUN': '1'})),
    ('shutdown command runs (rc 0)', dict(
        fn=scenario_command_ok, env={'AUTO_SHUTDOWN_MINUTES': '60',
                                     'AUTO_SHUTDOWN_CHECK_SECONDS': '0.1',
                                     'AUTO_SHUTDOWN_DRY_RUN': '0'})),
    ('failing command logs once, then backs off', dict(
        fn=scenario_command_fails, env={'AUTO_SHUTDOWN_MINUTES': '0.005',
                                        'AUTO_SHUTDOWN_CHECK_SECONDS': '0.1',
                                        'AUTO_SHUTDOWN_DRY_RUN': '0'})),
    ('AUTO_SHUTDOWN_MAX_HOURS forces it while busy', dict(
        fn=scenario_force_max_hours, env={'AUTO_SHUTDOWN_MINUTES': '0',
                                          'AUTO_SHUTDOWN_MAX_HOURS': '0.0002',
                                          'AUTO_SHUTDOWN_CHECK_SECONDS': '0.1',
                                          'AUTO_SHUTDOWN_DRY_RUN': '1'})),
]


def _child_env(name: str) -> dict:
    cfg = next(cfg for n, cfg in SCENARIOS if n == name)
    env = dict(os.environ)
    env.update(cfg['env'])
    env['PYTHONPATH'] = str(ROOT)
    env['MARKER_FILE'] = os.environ.get('MARKER_FILE', '/tmp/idle-smoke-marker')
    fn = cfg['fn']
    if fn is scenario_command_ok:
        env['AUTO_SHUTDOWN_COMMAND'] = f'sh -c "echo ran > {env["MARKER_FILE"]}"'
    elif fn is scenario_command_fails:
        env['AUTO_SHUTDOWN_COMMAND'] = 'false'
    elif 'AUTO_SHUTDOWN_DRY_RUN' not in env:
        env['AUTO_SHUTDOWN_COMMAND'] = 'true'
    return env


def main() -> int:
    # Child mode: run one scenario in this (freshly imported) process.
    if len(sys.argv) > 2 and sys.argv[1] == '_scenario':
        name = sys.argv[2]
        return next(cfg['fn'] for n, cfg in SCENARIOS if n == name)()

    failures: list[str] = []
    for name, _cfg in SCENARIOS:
        env = _child_env(name)
        if 'MARKER_FILE' in env and name.startswith('shutdown command'):
            Path(env['MARKER_FILE']).unlink(missing_ok=True)
        t0 = time.time()
        proc = subprocess.run([sys.executable, __file__, '_scenario', name],
                              env=env, capture_output=True, text=True, cwd=str(ROOT))
        result = ''
        for line in proc.stdout.splitlines():
            if line.startswith('RESULT '):
                result = line[len('RESULT '):]
        passed = proc.returncode == 0
        print(f'{"ok  " if passed else "FAIL"} {name}  ({time.time() - t0:.1f}s)')
        if result:
            print(f'       {result}')
        if not passed:
            failures.append(name)
            for line in (proc.stderr or '').strip().splitlines()[-6:]:
                print(f'       ! {line}')
            for line in (proc.stdout or '').strip().splitlines()[-4:]:
                print(f'       > {line}')

    print()
    if failures:
        print(f'{len(failures)} scenario(s) FAILED: ' + ', '.join(failures))
        return 1
    print('all idle-shutdown checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
