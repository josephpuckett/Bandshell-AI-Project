"""Long-running agent orchestration (the two state machines).

- `run_onboarding`: turn a website into a scripted site. Manual fill -> human
  approval -> write script -> run script -> human approval, looping back on
  failure/rejection.
- `run_task`: a one-off manual agent fill tied to a single event (no script).

These read/write the shared registries in `state` and drive the `Bot` (LLM)
through the mounted browser MCP agent (`parent`).
"""
import asyncio
import json
import re
from dataclasses import replace as dataclass_replace
from datetime import datetime
from pathlib import Path

from fastmcp import Client

from bot import Bot
from browser import clear_live_page, manager
from eventbrite_api import check_direct_image_url, direct_image_url
from scripter import (codegen_prompt, extract_code, extract_tool_calls,
                      initial_task_message, initial_website_message, strip_aria)
from scripting import (SANDBOX_ERROR_PREFIX, run_script_against_event,
                      validate_script)
from serialization import current_settings_version
from state import (MODEL, TOKEN, URL, DONE_WORDS, FALLBACK_MODELS, events,
                   input_queues, parent, tasks, website_queues, websites)
from storage import storage
from website import Script


# Directory where agent transcripts are persisted as JSONL (one message per line).
LOGS_DIR = Path(__file__).resolve().parent / 'logs'
# Directory where raw LLM codegen output is dumped for inspection.
GENERATED_DIR = Path(__file__).resolve().parent / 'generated'
# Cap on codegen retries so a degrading model doesn't loop forever.
MAX_WRITE_ATTEMPTS = 8


def dump_transcript(kind: str, obj_id, log: list) -> str:
    """Write a full agent/LLM transcript to logs/ as JSONL, best-effort.

    Returns the filename written (or '' on failure). Timestamped so multiple
    runs for the same website/task don't clobber each other.
    """
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = f'{kind}_{ts}_{str(obj_id)[:8]}.jsonl'
        with open(LOGS_DIR / fname, 'w', encoding='utf-8') as f:
            for m in log:
                f.write(json.dumps(m, default=str, ensure_ascii=False) + '\n')
        return fname
    except Exception:
        return ""


def log_note(website, text: str):
    website.log.append({'role': 'user', 'content': text})
    # Persist website state changes (status/phase/scripts/log) to the storage
    # backend if one is configured — log_note is called on every state change.
    storage.mark_dirty()


def dump_codegen(raw_text: str, code: str, reason: str = '') -> str:
    """Write the raw LLM answer and the extracted code to timestamped files.

    Returns the path of the written file (for logging). This is a debugging aid
    for inspecting what the model actually wrote, especially when script
    generation keeps failing.
    """
    GENERATED_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    stamp = re.sub(r'[^A-Za-z0-9]+', '_', reason)[:40] if reason else 'raw'
    base = GENERATED_DIR / f'{ts}_{stamp}'
    (base.parent / f'{base.name}.raw.txt').write_text(raw_text or '', encoding='utf-8')
    (base.parent / f'{base.name}.extracted.py').write_text(code or '', encoding='utf-8')
    return f'{base.name}.raw.txt'


async def wait_action(queue, actions):
    """Wait for an allowed action from the queue, returning (action, feedback).

    Queue items are dicts shaped {'action': ..., 'feedback': ...} so a human's
    optional rejection reason travels with the action. A special 'cancel' action
    is always honored (returns ('cancel', feedback)) so a stuck onboarding can
    be aborted from the UI.
    """
    while True:
        item = await queue.get()
        action = item.get('action') if isinstance(item, dict) else item
        feedback = item.get('feedback') if isinstance(item, dict) else ''
        if action == 'cancel':
            return 'cancel', feedback
        if action in actions:
            return action, feedback


async def _write_valid_script(b: Bot, website, event, feedback: str):
    """Generate a statically-valid script.

    Appends the codegen request (first call) or a rewrite request (on feedback)
    to the conversation, then keeps generating until the script passes
    `validate_script`. Returns (code, raw_output).
    """
    bot_log: list = b.log
    website.phase = 'writing'
    if not feedback:
        bot_log.append({'role': 'user',
                        'content': codegen_prompt(event, extract_tool_calls(bot_log))})
    else:
        bot_log.append({'role': 'user', 'content': feedback})

    attempts = 0
    while True:
        msg = await b.generate_loop()
        attempts += 1
        raw = str((msg.get('content') or ''))
        code = strip_aria(extract_code(raw))
        ok, reason = validate_script(code)
        if ok:
            fname = dump_codegen(raw, code, 'approved')
            log_note(website,
                     f'Script generated and passed static validation (dump: {fname}).')
            return code, raw
        fname = dump_codegen(raw, code, reason)
        log_note(website,
                 f'Generated script rejected: {reason} — retrying. (raw dump: {fname})')
        if attempts >= MAX_WRITE_ATTEMPTS:
            raise RuntimeError(
                f'could not generate a valid script after {attempts} attempts '
                f'(last: {reason}). Check generated/ dumps.')
        bot_log.append({'role': 'user', 'content':
            f'The script you just wrote was rejected: {reason}.\n'
            'Fix ONLY that problem. Rules: define async def run(event, api); '
            'keep the same roles/indices and api methods; helper defs are fine; '
            'no imports/no network/file. Return ONLY a single python code '
            'block, nothing else, no commentary.'})


class _OnboardingCancelled(Exception):
    """Internal sentinel: raises out of run_onboarding to abort cleanly."""
    pass


async def _image_warning(url: str) -> str:
    """Return a human-readable warning if a URL doesn't serve image bytes, else ''.

    Best-effort preflight: a Drive file that isn't publicly shared serves HTML
    instead of image bytes, which silently breaks the calendar site's own image
    fetch later. Call before letting the agent/script fill an image field.
    """
    if not url:
        return ''
    ok_img, why = await asyncio.to_thread(check_direct_image_url, url)
    if ok_img:
        return ''
    return (f'Image URL check failed: {why}. The calendar site may reject the image. '
            'If it\'s a Google Drive link, share the file as "Anyone with the link" (Viewer).')


async def run_onboarding(website_id, event_id):
    website = websites[website_id]
    event = events[event_id]
    # Present the agent a directly-fetchable image URL (Drive share links are
    # rewritten to a direct-download form, like the Eventbrite path). The agent's
    # manual fill and the generated script (which reads event.image at run time)
    # then both use the direct URL.
    event = dataclass_replace(event, image=direct_image_url(event.image))
    warn = await _image_warning(event.image)
    if warn:
        log_note(website, f'[warn] {warn}')
    queue = website_queues[website_id]
    website.settings_version = current_settings_version()
    website.status = 'scripting'
    log_note(website, 'Beginning onboarding.')

    bot_log: list = []
    client = Client(parent)
    try:
        async with client:
            tool_list = await client.session.list_tools()
            b = Bot(client, tool_list, model=MODEL, url=URL, token=TOKEN,
                    log=bot_log, fallback_models=FALLBACK_MODELS)

            # --- MANUAL FILL (repeat until approved) ---
            while True:
                website.phase = 'manual_fill'
                log_note(website, 'Agent is filling the form manually...')
                await b.chat({'role': 'user', 'content': initial_website_message(website, event)})
                await b.generate_loop()
                website.phase = 'manual_approval'
                log_note(website, '[waiting: approve or reject the manual fill]')
                action, _ = await wait_action(queue, {'approve', 'reject'})
                if action == 'cancel':
                    raise _OnboardingCancelled()
                if action == 'approve':
                    break
                log_note(website, 'Manual fill rejected — retrying manually.')

            # The agent's manual interaction is done and approved: close its
            # browser session now, so the stale manual-fill window doesn't stay
            # open (and confuse the review) while the script is written/run. The
            # scripted run opens its own fresh context/page next.
            try:
                await manager.close_session()
            except Exception:
                pass

            log_note(website, 'Manual fill approved. Writing reusable script...')

            # Initial script generation (no feedback yet).
            code, raw = await _write_valid_script(b, website, event, '')
            script = Script(code=code, settings_version=website.settings_version)
            website.scripts.append(script)

            # --- RUN SCRIPT (loop until the run is approved) ---
            # If execution raises a runtime error, or a human rejects the run, the
            # concrete reason is handed back to the agent to rewrite the script,
            # rather than silently re-running the same one.
            while True:
                website.phase = 'running'
                log_note(website, 'Running generated script in browser...')
                ok, msg2, page, context = await run_script_against_event(
                    script, event, live_key=website_id)
                if not ok:
                    if msg2.startswith(SANDBOX_ERROR_PREFIX):
                        # The sandbox itself failed before running any of the
                        # generated code — asking the LLM to rewrite the script
                        # would burn attempts on an infra problem. Surface it.
                        raise RuntimeError(msg2)
                    feedback = (
                        f'The generated script failed while running: {msg2}.\n'
                        'Fix the script based on that runtime error so it succeeds.\n'
                        'Keep the same roles/indices and api methods; helper defs are fine;\n'
                        'be more careful with dropdowns/comboboxes; NO api.aria() calls.\n'
                        'Return ONLY a single python code block, nothing else.')
                    log_note(website, f'Script execution failed: {msg2} — rewriting the script.')
                    code, raw = await _write_valid_script(b, website, event, feedback)
                    script = Script(code=code, settings_version=website.settings_version)
                    website.scripts.append(script)
                    continue

                website.phase = 'script_approval'
                log_note(website, '[waiting: approve or reject the scripted run]')
                action, feedback = await wait_action(queue, {'approve', 'reject'})
                if action == 'cancel':
                    # Dispose the review context, then abort.
                    if context is not None:
                        try:
                            await context.close()
                        except Exception:
                            pass
                    raise _OnboardingCancelled()
                # Dispose the review context once the human has passed judgment.
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                clear_live_page(website_id)
                if action == 'approve':
                    break
                reason = feedback or 'the fields, roles, or indices may be wrong'
                feedback_msg = (
                    'A human reviewed the filled form in the browser and rejected it.\n'
                    f'The reason given: {reason}\n'
                    'Fix the script accordingly. Keep the same api methods; helper defs\n'
                    'are fine; NO api.aria() calls. Return ONLY a single python code '
                    'block, nothing else.')
                log_note(website,
                         f'Scripted run rejected by human ({reason}) — rewriting the script.')
                code, raw = await _write_valid_script(b, website, event, feedback_msg)
                script = Script(code=code, settings_version=website.settings_version)
                website.scripts.append(script)

            script.status = 'approved'
            website.status = 'scripted'
            website.phase = 'done'
            log_note(website, 'Onboarding complete. Script approved for this website.')
    except asyncio.CancelledError:
        # runner.cancel() lands here (mid-generate). Reset so it can be restarted.
        website.status = 'unscripted'
        website.phase = 'idle'
        website.scripts = []
        website.settings_version = ''
        log_note(website, 'Onboarding cancelled by user.')
        raise
    except _OnboardingCancelled:
        # Human aborted: reset the site cleanly so onboarding can be restarted.
        website.status = 'unscripted'
        website.phase = 'idle'
        website.scripts = []
        website.settings_version = ''
        log_note(website, 'Onboarding cancelled by user.')
    except Exception as e:
        website.status = 'error'
        website.phase = 'error'
        log_note(website, f'[error] {e}')
    finally:
        # Drop the live-page pointer and close the agent's browser session(s)
        # so the window/tab doesn't stay open after onboarding ends/cancels.
        clear_live_page(website_id)
        try:
            await manager.close_session()
        except Exception:
            pass
        # Persist the full agent transcript for debugging/auditing.
        fname = dump_transcript('onboarding', website_id, bot_log)
        if fname:
            log_note(website, f'Transcript saved to logs/{fname}.')


async def run_task(task_id):
    task = tasks.get(task_id)
    if task is None:
        return
    task.status = 'running'
    event = events[task.event_id]
    # Same Drive-link conversion the onboarding path applies: the agent should
    # see (and fill) a directly-fetchable image URL.
    event = dataclass_replace(event, image=direct_image_url(event.image))
    warn = await _image_warning(event.image)
    if warn:
        task.log.append({'role': 'user', 'content': f'[warn] {warn}'})
    client = Client(parent)
    try:
        async with client:
            tool_list = await client.session.list_tools()
            b = Bot(client, tool_list, model=MODEL, url=URL, token=TOKEN,
                    log=task.log, fallback_models=FALLBACK_MODELS)

            await b.chat({'role': 'user', 'content': initial_task_message(task, event)})
            await b.generate_loop()
            task.status = 'waiting'

            while True:
                content = await input_queues[task_id].get()
                if content.strip().lower() in DONE_WORDS:
                    break
                task.status = 'running'
                await b.chat({'role': 'user', 'content': content})
                await b.generate_loop()
                task.status = 'waiting'
        task.status = 'done'
    except asyncio.CancelledError:
        raise
    except Exception as e:
        task.status = 'error'
        task.log.append({'role': 'user', 'content': f'[error] {e}'})
    finally:
        # Persist the full agent transcript for debugging/auditing.
        dump_transcript('task', task_id, task.log)
        # Close the task's browser session so the window doesn't stay open
        # after the one-off task finishes (mirrors run_onboarding's finally).
        try:
            await manager.close_session()
        except Exception:
            pass