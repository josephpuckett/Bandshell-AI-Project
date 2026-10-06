"""Storage abstraction: in-memory by default, Google Sheets when env configured.

The in-memory registries in `state` remain the runtime source of truth; this
layer provides load/save to a persistent backend. Mutations call `mark_dirty()`
and a background task flushes a snapshot to the backend on a short interval, so
background agent changes (status/scripts/log) are persisted too.
"""
import asyncio
import os


class Storage:
    def __init__(self, impl):
        self.impl = impl
        self._dirty = False
        self._task: asyncio.Task | None = None

    def mark_dirty(self):
        self._dirty = True

    @property
    def backend(self):
        return type(self.impl).__name__

    async def load(self):
        await self.impl.load()

    async def flush(self):
        if self._dirty:
            await self.impl.save()
            self._dirty = False

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._watchdog())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self.flush()

    async def _watchdog(self):
        while True:
            try:
                await asyncio.sleep(5)
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # storage errors must not kill the server


def make_storage() -> Storage:
    """Sheets storage if env vars are set, otherwise a no-op in-memory backend."""
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.getenv("EVENTS_SPREADSHEET_ID"):
        from drive import DriveStorage
        return Storage(DriveStorage())
    from memory_storage import MemoryStorage
    return Storage(MemoryStorage())


storage = make_storage()