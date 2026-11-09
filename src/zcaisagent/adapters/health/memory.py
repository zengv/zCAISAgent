import time

from zcaisagent.ports.health import HealthEntry, HealthStatus


class HealthBoard:
    """One entry per part, claimed by its single writer."""

    def __init__(self) -> None:
        self._entries: dict[str, HealthEntry] = {}

    def register(self, name: str, owner: str) -> None:
        if name in self._entries and self._entries[name].owner != owner:
            raise ValueError(f"health entry already claimed: {name}")
        self._entries[name] = HealthEntry(name=name, owner=owner, updated_at=time.time())

    def report(
        self, name: str, status: HealthStatus, message: str = ""
    ) -> None:
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"health entry not registered: {name}")
        entry.status = status
        entry.message = message
        entry.updated_at = time.time()

    def snapshot(self) -> list[HealthEntry]:
        return [
            entry.model_copy(deep=True)
            for entry in sorted(self._entries.values(), key=lambda e: e.name)
        ]
