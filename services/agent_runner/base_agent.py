"""Base class for all JARVIS background agents."""

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import redis


class BaseAgent(ABC):
    """All background agents inherit from this class.

    Subclasses must implement ``run()``, which performs the agent's task and
    returns a plain-text report string.  The runner calls ``store_report()``
    automatically after a successful run.
    """

    def __init__(self, name: str, params: dict, r: redis.Redis):
        self.name = name
        self.params = params
        self.r = r

    @abstractmethod
    async def run(self) -> str:
        """Execute the agent task. Return a plain-text report."""

    def store_report(self, report: str) -> None:
        """Persist the report in Redis (keeps the last 50 per agent)."""
        entry = json.dumps(
            {
                "agent": self.name,
                "report": report,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        key = f"agent:{self.name}:reports"
        self.r.lpush(key, entry)
        self.r.ltrim(key, 0, 49)
        self.r.set(
            f"agent:{self.name}:last_run",
            datetime.now(timezone.utc).isoformat(),
        )
