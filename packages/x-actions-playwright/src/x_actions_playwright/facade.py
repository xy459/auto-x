from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .adapter import XAdapter
from .catalog import ACTION_CATEGORIES, ACTION_DEFINITIONS, get_action_definition, list_actions
from .errors import ActionError, normalize_error
from .idempotency import IdempotencyStore, MemoryIdempotencyStore
from .models import ActionDefinition, ActionResult, ExecutionOptions
from .workflow import WorkflowRunner


class ActionNamespace:
    def __init__(self, actions: XActions, category: str) -> None:
        self._actions = actions
        self._category = category
        for definition in list_actions(category):
            setattr(self, definition.method, self._method(definition.id))

    def _method(self, action_id: str) -> Any:
        async def invoke(page: Any, payload: dict[str, Any] | None = None, options: dict[str, Any] | ExecutionOptions | None = None) -> ActionResult:
            return await self._actions.execute(page, action_id, payload, options)

        invoke.__name__ = action_id.split(".", 1)[1]
        return invoke


class XActions:
    def __init__(
        self,
        *,
        adapter: XAdapter | None = None,
        idempotency_store: IdempotencyStore | None = None,
        default_timeout_ms: int = 10_000,
    ) -> None:
        self.adapter = adapter or XAdapter()
        self.idempotency_store = idempotency_store or MemoryIdempotencyStore()
        self.default_timeout_ms = default_timeout_ms
        self._page_locks: dict[int, asyncio.Lock] = {}
        for category in ACTION_CATEGORIES:
            setattr(self, category, ActionNamespace(self, category))
        self.workflow = WorkflowRunner(self)

    def get_action_definition(self, action_id: str) -> ActionDefinition | None:
        return get_action_definition(action_id)

    def list_actions(self, category: str | None = None) -> list[ActionDefinition]:
        return list_actions(category)

    def _options(self, raw: dict[str, Any] | ExecutionOptions | None, payload: dict[str, Any]) -> ExecutionOptions:
        if isinstance(raw, ExecutionOptions):
            options = raw
        else:
            value = raw or {}
            options = ExecutionOptions(
                dry_run=bool(value.get("dryRun", value.get("dry_run", payload.get("dryRun", False)))),
                confirm_live=bool(value.get("confirmLive", value.get("confirm_live", payload.get("confirmLive", False)))),
                idempotency_key=value.get("idempotencyKey", value.get("idempotency_key", payload.get("idempotencyKey"))),
                timeout_ms=int(value.get("timeoutMs", value.get("timeout_ms", payload.get("timeoutMs", self.default_timeout_ms)))),
                cancellation=value.get("cancellation"),
                account_scope=value.get("accountScope", value.get("account_scope")),
                capture_failure=bool(value.get("captureFailure", value.get("capture_failure", False))),
                artifact_hook=value.get("artifactHook", value.get("artifact_hook")),
            )
        options.timeout_ms = max(250, min(int(options.timeout_ms), 120_000))
        if options.idempotency_key is not None:
            if not isinstance(options.idempotency_key, str) or not options.idempotency_key.strip():
                raise ActionError("CONTENT_MISMATCH", "idempotencyKey must be a non-empty string.")
            options.idempotency_key = options.idempotency_key.strip()[:200]
        return options

    def _validate_payload(self, definition: ActionDefinition, payload: dict[str, Any]) -> None:
        if definition.requires_tweet and not payload.get("tweetId") and not self.adapter.selected_tweet_id:
            raise ActionError("TARGET_NOT_FOUND", f"Action {definition.id} requires tweetId or a selected post.")
        if definition.requires_comment and not payload.get("commentId"):
            raise ActionError("TARGET_NOT_FOUND", f"Action {definition.id} requires commentId.")
        for required in definition.input_schema.get("required", []):
            if required not in payload and not (required == "tweetId" and self.adapter.selected_tweet_id):
                raise ActionError("CONTENT_MISMATCH", f"Action {definition.id} requires payload.{required}.")

    def _idempotency_key(self, definition: ActionDefinition, options: ExecutionOptions) -> str | None:
        if definition.access != "write" or options.dry_run or not options.confirm_live or not options.idempotency_key:
            return None
        scope = options.account_scope or "default"
        return f"{scope}:{definition.id}:{options.idempotency_key}"

    async def execute(
        self,
        page: Any,
        action_id: str,
        payload: dict[str, Any] | None = None,
        options: dict[str, Any] | ExecutionOptions | None = None,
    ) -> ActionResult:
        definition = ACTION_DEFINITIONS.get(action_id)
        if not definition:
            raise ActionError("ACTION_UNSUPPORTED", f"Unsupported action: {action_id}", {"action": action_id})
        if not definition.enabled:
            raise ActionError("ACTION_UNSUPPORTED", definition.disabled_reason or f"Action {action_id} is disabled.")
        body = dict(payload or {})
        execution = self._options(options, body)
        if execution.cancellation and execution.cancellation.is_set():
            raise ActionError("USER_CANCELLED", f"Action {action_id} was cancelled before execution.")
        if definition.access == "write" and not execution.dry_run and not execution.confirm_live:
            raise ActionError("CONFIRMATION_REQUIRED", f"Action {action_id} requires confirmLive=true.")
        self._validate_payload(definition, body)
        store_key = self._idempotency_key(definition, execution)
        if store_key:
            previous = await self.idempotency_store.get(store_key)
            if previous:
                return self._envelope(definition, {"status": "skipped", "reason": "idempotency-key-reused", "previous": previous}, execution, 0)
            await self.idempotency_store.put(store_key, {"state": "pending", "action": action_id, "startedAt": datetime.now(UTC).isoformat()})

        started = asyncio.get_running_loop().time()
        lock = self._page_locks.setdefault(id(page), asyncio.Lock())
        try:
            async with lock:
                raw = await asyncio.wait_for(
                    self.adapter.dispatch(page, definition.handler, body, execution),
                    timeout=execution.timeout_ms / 1000 + 0.5,
                )
            duration = round((asyncio.get_running_loop().time() - started) * 1000)
            result = self._envelope(definition, raw, execution, duration)
            if store_key:
                await self.idempotency_store.put(store_key, {"state": result.status, "action": action_id, "completedAt": datetime.now(UTC).isoformat()})
            return result
        except asyncio.TimeoutError as error:
            normalized = ActionError("TIMEOUT", f"Action {action_id} exceeded {execution.timeout_ms} ms.", retryable=definition.retry_policy == "safe")
            if store_key:
                await self.idempotency_store.delete(store_key)
            await self._capture(page, definition, normalized, execution)
            raise normalized from error
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                normalized = ActionError("USER_CANCELLED", f"Action {action_id} was cancelled.")
            else:
                normalized = normalize_error(error)
            if store_key and not normalized.uncertain:
                await self.idempotency_store.delete(store_key)
            await self._capture(page, definition, normalized, execution)
            raise normalized from error

    async def _capture(self, page: Any, definition: ActionDefinition, error: ActionError, options: ExecutionOptions) -> None:
        if not options.capture_failure or not options.artifact_hook:
            return
        try:
            result = options.artifact_hook(page=page, action=definition.id, error=error.to_dict())
            if hasattr(result, "__await__"):
                await result
        except Exception:
            return

    def _envelope(self, definition: ActionDefinition, raw: dict[str, Any], options: ExecutionOptions, duration_ms: int) -> ActionResult:
        status = raw.get("status", "success")
        data = {key: value for key, value in raw.items() if key not in {"status", "evidence", "warnings"}}
        warnings = list(raw.get("warnings") or [])
        if definition.deprecated:
            warnings.append(f"Action {definition.id} is deprecated; use {definition.replaced_by}.")
        return ActionResult(
            status=status,
            action=definition.id,
            category=definition.category,
            data=data,
            evidence=list(raw.get("evidence") or []),
            warnings=warnings,
            meta={
                "durationMs": duration_ms,
                "access": definition.access,
                "retryPolicy": definition.retry_policy,
                "targetType": definition.target_type,
                "confirmation": definition.confirmation,
                "idempotent": definition.idempotent,
                "enabled": definition.enabled,
                "deprecated": definition.deprecated,
                "replacedBy": definition.replaced_by,
                "idempotencyKey": options.idempotency_key,
            },
        )
