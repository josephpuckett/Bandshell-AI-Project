from mcp.types import ListToolsResult
import json
import asyncio
import time
import httpx
from pathlib import Path
from fastmcp import Client

LOGS_DIR = Path(__file__).resolve().parent / 'logs'


def _append_usage_record(record: dict):
    """Append one JSON line to logs/llm_usage.jsonl (best-effort)."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        with open(LOGS_DIR / 'llm_usage.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, default=str) + '\n')
    except Exception:
        pass  # never let logging break an LLM call

# Cap on output tokens per completion. Without this a model can run away and
# emit tens of thousands of tokens in one turn (which bloats cost, and — if the
# response exceeds the client read timeout — gets dropped entirely without ever
# being fed back into the conversation). Tool-call turns are short; script
# codegen needs a few KB at most. 8192 is generous for both.
MAX_TOKENS = 8192

# Per-request timeouts. The read timeout must comfortably cover the slowest
# generation we allow (MAX_TOKENS at a slow provider rate), otherwise a large
# but *valid* response gets abandoned mid-stream.
TIMEOUT = httpx.Timeout(connect=10, read=600, write=30, pool=10)

# Retry budget for *transient* failures (429 rate limits, upstream 5xx).
# OpenRouter explicitly says to wait + bulk-backoff rather than blast retries.
MAX_ATTEMPTS = 4


class _TransientError(Exception):
    """A retryable failure (rate limit / upstream 5xx) with an optional
    Retry-After hint (seconds). Swallowed by the retry loop."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def _error_text(data) -> str:
    """Pull a human-readable message out of an OpenRouter error body.
    Accepts either a parsed dict or a JSON string."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return data or ''
    if isinstance(data, dict):
        err = data.get('error') or {}
        if isinstance(err, dict):
            msg = err.get('message') or ''
            code = err.get('code')
            return f'{msg} (code={code})' if code else msg
        return str(err)
    return str(data)


def _parse_retry_after(resp) -> float | None:
    """Parse a Retry-After header (integer seconds, or an HTTP-date) into
    a wait in seconds, or None when absent/unparseable."""
    raw = resp.headers.get('retry-after') if hasattr(resp, 'headers') else None
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    except Exception:
        return None


def tool_message(content, tool_call_id=None):
    """Helper: build a `role: tool` message for the assistant's log."""
    msg = {
        'role': 'tool',
        'content': content
    }
    # OpenAI-compatible providers require each tool result to reference the
    # id of the assistant call it answers (otherwise HTTP 400). DeepSeek's
    # endpoint tolerated a missing id; GPT-family models do not.
    if tool_call_id:
        msg['tool_call_id'] = tool_call_id
    return msg

class Bot:
    def __init__(self, client: Client, tool_list: ListToolsResult, model, url, token="dummy", log=[], fallback_models=None):
        self.url = url
        self.token = token
        self.model = model
        self.fallback_models = list(fallback_models or [])
        self.client = client
        self.tools = {tool.name: tool for tool in tool_list.tools}
        self.log = log

    async def call_tool(self, call, tool_call_id=None):
        name = call['name']
        args = call['arguments']
        if name not in self.tools:
            return tool_message(f'Unknown tool: {name}', tool_call_id)
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return tool_message('Failed to decode arguments', tool_call_id)
        try:
            print(f'tool: {name}({args})')
            result = await self.client.call_tool(name, args)
            message_content = []
            for content in result.content:
                if content.type == 'text':
                    message_content.append({
                        'type': 'text',
                        'text': content.text
                    })
                elif content.type == 'image':
                    message_content.append({
                        'type': 'image_url',
                        'image_url': {
                            'url': f'data:{content.mimeType};base64,{content.data}'
                        }
                    })
            return tool_message(message_content, tool_call_id)
        except asyncio.CancelledError:
            raise  # cancellation must propagate so the agent stops
        except Exception as e:
            # Include the real reason (missing element, timeout, strict-mode
            # ambiguity...) so the agent can self-correct instead of guessing.
            return tool_message(f'Failed to run tool: {name}: {e}', tool_call_id)

    async def generate(self) -> dict:
        # OpenRouter fallback routing: try self.model first, then any fallbacks
        # (e.g. a rate-limited gpt-6-luna fails over to deepseek). With no
        # fallbacks, send plain `model` (some providers choke on `models`).
        payload = {
            'messages': self.log,
            'tools': [{
                'type': 'function',
                'function': {
                    'name': t.name,
                    'description': t.description,
                    'parameters': t.inputSchema
                }
            } for t in self.tools.values()],
            'tool_choice': 'auto',
            'max_tokens': MAX_TOKENS,
        }
        if self.fallback_models:
            payload['models'] = [self.model] + list(self.fallback_models)
        else:
            payload['model'] = self.model
        headers = {
            "Authorization": f"Bearer {self.token}"
        }
        # Use an async HTTP client so the request is genuinely cancellable — a
        # blocking to_thread() call can't be interrupted, which meant cancelling
        # an onboarding didn't stop the agent mid-LLM-call and it kept making
        # (now-failing) tool calls.
        last_err = None
        data = None
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
            for attempt in range(MAX_ATTEMPTS):
                started = time.monotonic()
                try:
                    resp = await client.post(
                        self.url + '/v1/chat/completions',
                        json=payload)
                    if resp.status_code == 429:
                        # Rate limited (platform or upstream): transient, honor
                        # Retry-After when present, back off, and retry.
                        raise _TransientError(
                            _error_text(resp.text)
                            or f'HTTP 429 rate-limited: {resp.text[:200]}',
                            _parse_retry_after(resp))
                    if resp.status_code >= 500:
                        raise _TransientError(f'HTTP {resp.status_code}: {resp.text[:200]}')
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f'HTTP {resp.status_code}: {resp.text[:300]}')
                    data = resp.json()
                    # OpenRouter reports some provider errors (esp. 429s) in-band:
                    # HTTP 200 with an {"error": {...}} body instead of choices.
                    if data.get('error'):
                        err = data['error']
                        code = err.get('code')
                        msg = err.get('message') or str(err)
                        if code == 429:
                            raise _TransientError(msg)
                        raise RuntimeError(f'error in response body: {msg}')
                    elapsed = time.monotonic() - started
                    self._log_usage(data, elapsed)
                    break
                except asyncio.CancelledError:
                    raise
                except _TransientError as e:
                    last_err = e
                    elapsed = time.monotonic() - started
                    print(f'[llm] attempt {attempt + 1} throttled: {e} '
                          f'(will retire in {1.5 * (attempt + 1):.1f}s max)')
                    _append_usage_record({
                        'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        'model': self.model,
                        'ok': False,
                        'attempt': attempt + 1,
                        'error': f'{e}',
                        'elapsed': round(elapsed, 1),
                        'transient': True,
                        'retry_after': e.retry_after,
                    })
                    await asyncio.sleep(min(e.retry_after or 2.0, 30.0) if e.retry_after
                                        else 1.5 * (attempt + 1))
                except Exception as e:
                    last_err = e
                    elapsed = time.monotonic() - started
                    print(f'[llm] attempt {attempt + 1} failed after '
                          f'{elapsed:.1f}s: {e!r}')
                    # Persist failed attempts too — they're cost/troubleshooting data.
                    _append_usage_record({
                        'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        'model': self.model,
                        'ok': False,
                        'attempt': attempt + 1,
                        'error': f'{e!r}',
                        'elapsed': round(elapsed, 1),
                    })
                    await asyncio.sleep(1.5 * (attempt + 1))
        if data is None:
            raise RuntimeError(
                f'LLM request to {self.url} failed after {MAX_ATTEMPTS} attempts: {last_err} '
                '(is the LLM server running and the model name correct?)'
            )
        if 'choices' not in data or not data.get('choices'):
            raise RuntimeError(f'LLM returned no choices: {json.dumps(data)[:300]}')
        response = data['choices'][0]['message']
        self.log.append(response)
        if 'tool_calls' in response and response.get('tool_calls'):
            for tool_call in response['tool_calls']:
                function = tool_call['function']
                tool_response = await self.call_tool(function, tool_call.get('id'))
                self.log.append(tool_response)
        return self.log[-1]

    @staticmethod
    def _log_usage(data: dict, elapsed: float):
        """Print + persist finish_reason and token usage for observability."""
        try:
            usage = data.get('usage') or {}
            finish = (data.get('choices') or [{}])[0].get('finish_reason')
            print('[llm] finish=%s in=%s out=%s elapsed=%.1fs' % (
                finish,
                usage.get('prompt_tokens'),
                usage.get('completion_tokens'),
                elapsed,
            ))
            _append_usage_record({
                'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'model': data.get('model'),
                'ok': True,
                'finish': finish,
                'prompt_tokens': usage.get('prompt_tokens'),
                'completion_tokens': usage.get('completion_tokens'),
                'elapsed': round(elapsed, 1),
            })
        except Exception:
            pass
    
    async def chat(self, message):
        self.log.append(message)
        return await self.generate()
    
    async def generate_loop(self):
        while True:
            message = await self.generate()
            if message['role'] == 'tool':
                continue
            else:
                break
        return message
            
