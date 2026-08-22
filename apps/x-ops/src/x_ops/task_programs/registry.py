from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ..task_sdk import TaskContext
from .spec import ProgramSpec


@dataclass(frozen=True, slots=True)
class TaskProgram:
    SPEC: ProgramSpec
    Params: type[BaseModel]
    run: Callable[[TaskContext, Any], Awaitable[dict[str, Any]]]


class TaskProgramRegistry:
    def __init__(self) -> None:
        self._programs: dict[str, TaskProgram] = {}

    def register(self, program: TaskProgram) -> None:
        if program.SPEC.name in self._programs:
            raise ValueError(f"duplicate task program: {program.SPEC.name}")
        self._programs[program.SPEC.name] = program

    def get(self, name: str) -> TaskProgram | None:
        return self._programs.get(name)

    def require(self, name: str) -> TaskProgram:
        program = self.get(name)
        if not program:
            raise KeyError(name)
        return program

    def list(self) -> tuple[TaskProgram, ...]:
        return tuple(self._programs[name] for name in sorted(self._programs))

    def describe(self) -> builtins.list[dict[str, Any]]:
        return [
            {
                "name": program.SPEC.name,
                "version": program.SPEC.version,
                "title": program.SPEC.title,
                "description": program.SPEC.description,
                "supports_batch_schedule": program.SPEC.supports_batch_schedule,
                "params_schema": program.Params.model_json_schema(),
            }
            for program in self.list()
        ]

    @classmethod
    def default(cls) -> TaskProgramRegistry:
        from . import (
            browse_match_engage,
            browse_only,
            browse_view_posts,
            like_posts,
            login_accounts,
            reply_posts,
            search_authors_engage,
        )

        registry = cls()
        for module in (
            browse_only,
            like_posts,
            reply_posts,
            browse_match_engage,
            search_authors_engage,
            login_accounts,
            browse_view_posts,
        ):
            registry.register(TaskProgram(module.SPEC, module.Params, module.run))
        return registry
