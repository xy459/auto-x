from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .account import AccountContext
from .ai import AIService
from .cancellation import CancellationToken
from .logging import TaskLogger


@dataclass(frozen=True, slots=True)
class TaskContext:
    """The complete and intentionally limited Task Program SDK surface."""

    account: AccountContext
    actions: Any
    ai: AIService
    logger: TaskLogger
    cancellation: CancellationToken
