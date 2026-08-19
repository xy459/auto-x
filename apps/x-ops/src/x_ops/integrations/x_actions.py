from __future__ import annotations

import re
from typing import Any


def _camel(name: str) -> str:
    return re.sub(r"_([a-z])", lambda match: match.group(1).upper(), name)


class _BoundNamespace:
    def __init__(self, namespace: Any, page: Any) -> None:
        self._namespace = namespace
        self._page = page

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._namespace, name)

        async def invoke(
            payload: dict[str, Any] | None = None,
            options: dict[str, Any] | Any | None = None,
            **kwargs: Any,
        ) -> Any:
            if payload is not None and kwargs:
                raise TypeError("pass either payload or keyword fields, not both")
            body = dict(payload or {_camel(key): value for key, value in kwargs.items()})
            return await method(self._page, body, options)

        return invoke


class BoundXActions:
    def __init__(self, actions: Any, page: Any) -> None:
        self._actions = actions
        self._page = page

    def __getattr__(self, name: str) -> Any:
        return _BoundNamespace(getattr(self._actions, name), self._page)

    async def execute(
        self,
        action_id: str,
        payload: dict[str, Any] | None = None,
        options: dict[str, Any] | Any | None = None,
    ) -> Any:
        return await self._actions.execute(self._page, action_id, payload, options)


class BoundXActionsFactory:
    def __init__(self, actions: Any | None = None) -> None:
        if actions is None:
            from x_actions_playwright import XActions

            actions = XActions()
        self._actions = actions

    def bind(self, page: Any) -> BoundXActions:
        return BoundXActions(self._actions, page)
