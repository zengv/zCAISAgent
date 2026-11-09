from __future__ import annotations

from dataclasses import dataclass, field

from zcaisagent.ports.retrieval import RetrieverPort
from zcaisagent.ports.security import AuthContext


@dataclass
class SourceRegistration:
    name: str
    retriever: RetrieverPort
    enabled: bool = True
    priority: int = 100


@dataclass
class SourceRegistry:
    """Registered retrieval sources; adding one must not change the kernel."""

    _sources: dict[str, SourceRegistration] = field(default_factory=dict)

    def register(
        self, retriever: RetrieverPort, priority: int = 100, enabled: bool = True
    ) -> None:
        name = retriever.source_name
        if name in self._sources:
            raise ValueError(f"source already registered: {name}")
        self._sources[name] = SourceRegistration(
            name=name, retriever=retriever, enabled=enabled, priority=priority
        )

    def list(self, enabled_only: bool = True) -> list[RetrieverPort]:
        items = sorted(
            self._sources.values(), key=lambda item: (item.priority, item.name)
        )
        return [
            item.retriever for item in items if item.enabled or not enabled_only
        ]

    def names(self) -> list[str]:
        return sorted(self._sources)

    def get(self, name: str) -> RetrieverPort | None:
        registration = self._sources.get(name)
        return registration.retriever if registration else None

    async def retrieve_all(
        self,
        request,
        ctx: AuthContext,
        timeout_s: float = 10.0,
    ) -> list:
        import asyncio

        tasks = [
            asyncio.wait_for(retriever.retrieve(request, ctx), timeout=timeout_s)
            for retriever in self.list()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results
