"""Narrow generation port. Providers return proposals, never mutate goal records."""
from typing import Protocol, Callable


class GoalGenerator(Protocol):
    def generate(self, versions: list[dict], *, check: Callable[[], None]) -> dict:
        """Return {nodes, generation}; each populated field needs exact evidence."""
        ...
