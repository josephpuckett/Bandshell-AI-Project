"""In-memory storage backend (the default).

No-op: the registries in `state` already start empty/current, so load/save do
nothing. Exists so the rest of the app can treat storage uniformly.
"""


class MemoryStorage:
    async def load(self):
        pass

    async def save(self):
        pass