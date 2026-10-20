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

# JPEG quality for live-view frames. Lower = faster encode + smaller frames
# (the encode is the CPU floor on small VMs), at a small visual cost. 70–80 is
# a good watching balance; 50–60 for constrained hardware.
BROWSER_JPEG_QUALITY = int(os.getenv("BROWSER_JPEG_QUALITY", "80") or 80)
BROWSER_JPEG_QUALITY = min(max(BROWSER_JPEG_QUALITY, 30), 95)

# --- Sandboxed script execution (bubblewrap) ---
# Generated scripts are untrusted (a malicious page can steer the agent into
# writing them), so they run inside bwrap with no network, no host filesystem
# and a scrubbed environment — see docs/sandbox-plan.md.
#   auto (default): sandbox when bwrap is installed, else in-process
#   1/on/require:   require bwrap (a missing bwrap fails the run loudly)
#   0/off:          always use the in-process executor (dev fallback)
SCRIPT_SANDBOX = (os.getenv("SCRIPT_SANDBOX") or "auto").strip().lower()
# bwrap binary (override for a non-PATH install).
BWRAP = os.getenv("BWRAP") or "bwrap"
# Wall-clock cap on one sandboxed run; the child is killed when it expires.
SANDBOX_TIMEOUT = float(os.getenv("SANDBOX_TIMEOUT", "120") or 120)
# Resource ceilings applied INSIDE the sandbox by sandbox_runner.py (bubblewrap
# has no --rlimit-* options). A runaway script's memory is host memory, so the
# address-space cap is what protects a small VM; it is passed in via --setenv.
SANDBOX_CPU_SECONDS = int(os.getenv("SANDBOX_CPU_SECONDS", "30") or 30)
SANDBOX_MAX_MEMORY_MB = int(os.getenv("SANDBOX_MAX_MEMORY_MB", "1024") or 1024)
# Which MCP client the sandboxed runner uses: auto (fastmcp if importable, else
# the mcp SDK), fastmcp, or mcp. `mcp` is the lighter path — on a 1GB VM the
# fastmcp import can be the peak-memory moment of a run (SIGKILL/exit 137).
SANDBOX_CLIENT = (os.getenv("SANDBOX_CLIENT") or "auto").strip().lower()

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
