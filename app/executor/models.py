"""Data models shared across the execution layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExecutionStatus(str, Enum):
    """Outcome of an execution attempt."""

    SUCCESS = "success"          # ran to completion, exit code 0
    ERROR = "error"             # user code raised / non-zero exit
    TIMEOUT = "timeout"         # wall-clock or CPU limit exceeded
    MEMORY = "memory"           # killed for exceeding the memory limit
    BLOCKED = "blocked"         # rejected by validation before running
    INTERNAL_ERROR = "internal"  # something went wrong in the runner harness


@dataclass
class ExecutionResult:
    """Result of running a snippet.

    Attributes are intentionally small and bounded (stdout/stderr are already
    capped by the executor) so results never retain large amounts of memory.
    """

    status: ExecutionStatus
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    truncated: bool = False
    # Optional human-readable message for BLOCKED / INTERNAL_ERROR states.
    message: str = ""
    # Generated images (e.g. from plt.show) as (filename, bytes)
    images: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def timed_out(self) -> bool:
        return self.status == ExecutionStatus.TIMEOUT

    @property
    def succeeded(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS
