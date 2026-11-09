from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class HealthEntry(BaseModel):
    name: str
    owner: str
    status: HealthStatus = HealthStatus.UNKNOWN
    message: str = ""
    updated_at: float = 0.0


class HealthPort(Protocol):
    def register(self, name: str, owner: str) -> None: ...

    def report(
        self, name: str, status: HealthStatus, message: str = ""
    ) -> None: ...

    def snapshot(self) -> list[HealthEntry]: ...
