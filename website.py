from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4, UUID


@dataclass
class Script:
    label: str = "enter_fields"      # only "enter_fields" for now (submit is manual)
    order: int = 0
    code: str = ""                   # generated python: async def run(event, api) -> None
    status: str = "draft"            # draft | approved | error
    settings_version: str = ""       # settings context the script was built against
    id: UUID = field(default_factory=uuid4)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# PHASES (current onboarding step / where the agent is waiting):
#   idle | manual_fill | manual_approval | writing | running | script_approval | done | error
# STATUS:
#   unscripted | scripting | scripted | stale | error
# KIND:
#   agentic (LLM + generated script) or eventbrite (API token, no LLM)
@dataclass
class Website:
    url: str
    name: str
    id: UUID = field(default_factory=uuid4)
    kind: str = "agentic"            # agentic | eventbrite
    status: str = "unscripted"
    phase: str = "idle"
    scripts: list[Script] = field(default_factory=list)  # ordered by .order
    settings_version: str = ""
    # Eventbrite-specific config (reused for every add-event call)
    venue_id: str = ""
    organizer_id: str = ""
    added_events: dict[str, str] = field(default_factory=dict)  # event_id -> eventbrite url
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    log: list = field(default_factory=list)  # onboarding/interaction log
