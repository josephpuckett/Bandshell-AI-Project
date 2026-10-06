"""Shared global state and configuration.

Centralizes the in-memory registries, the mounted browser MCP agent, and the
LLM provider config so other modules (main, agents, serialization) can share
them without circular imports.

NOTE: This is a demo/prototype — all state is in-memory and lost on restart.
A real deployment would swap these for a database / Google Drive storage layer.
"""
import asyncio
import os
from pathlib import Path
from uuid import UUID

from fastmcp import FastMCP

from browser import mcp as browser_mcp
from event import Event
from settings import Settings
from task import Task
from website import Website

# --- LLM provider config ---
MODEL = "deepseek/deepseek-v4.1-flash"
URL = "https://openrouter.ai/api"
TOKEN = os.getenv("OPENROUTER_API_KEY")
# Optional comma-separated fallback models tried when the primary is
# rate-limited/unavailable (OpenRouter 'models' array). E.g.
# OPENROUTER_FALLBACK_MODELS="openai/gpt-4o-mini,mistralai/mistral-small-3.1-24b-instruct"
FALLBACK_MODELS = [m.strip() for m in (os.getenv("OPENROUTER_FALLBACK_MODELS") or "").split(",") if m.strip()]

# --- Eventbrite API (personal token; used only server-side in the bearer header) ---
EVENTBRITE_TOKEN = os.getenv("EVENTBRITE_TOKEN") or ""

# --- Embedded live view frame rate ---
# Max frames per second streamed to the browser (JPEG screenshots of the live
# Playwright page). Each frame is a full viewport JPEG encode in the browser
# process — CPU-bound, so keep this low on small VMs. Default 4 is gentle;
# raise (e.g. 10) on faster hardware. Capped at 60 to avoid nonsense values.
BROWSER_FPS = float(os.getenv("BROWSER_FPS", "4") or 4.0)
BROWSER_FPS = min(max(BROWSER_FPS, 1.0), 60.0)

# --- The mounted browser MCP agent (Bot drives Playwright through this) ---
parent = FastMCP("parent")
parent.mount(browser_mcp)

# --- Filesystem (frontend assets) ---
FRONTEND = Path(__file__).resolve().parent / 'frontend'

# ---------------------------------------------------------------------------
# In-memory registries (a real deployment would use a database / google drive)
# ---------------------------------------------------------------------------
events: dict[UUID, Event] = {}
tasks: dict[UUID, Task] = {}
input_queues: dict[UUID, asyncio.Queue] = {}
runners: dict[UUID, asyncio.Task] = {}
websites: dict[UUID, Website] = {}
website_queues: dict[UUID, asyncio.Queue] = {}
website_runners: dict[UUID, asyncio.Task] = {}
settings: Settings = Settings()

DONE_WORDS = {'done', 'finish', '/done', 'approved', 'approve', 'accept'}
