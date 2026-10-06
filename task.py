from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4, UUID

# STATUS:
#   pending   - task created, runner not started yet
#   running   - agent is actively calling tools / waiting on the LLM
#   waiting   - agent finished a turn and is waiting for human input/approval
#   done      - human approved / task completed
#   error     - something went wrong (LLM unreachable, tool failed, etc.)
@dataclass
class Task:
    url: str
    event_id: UUID
    id: UUID = field(default_factory=uuid4)
    log: list = field(default_factory=list)
    status: str = 'pending'
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
