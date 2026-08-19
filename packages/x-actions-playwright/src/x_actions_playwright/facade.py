from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from weakref import WeakKeyDictionary, WeakValueDictionary

from .adapter import XAdapter
from .catalog import ACTION_CATEGORIES, ACTION_DEFINITIONS, get_action_definition, list_actions
from .errors import ActionError, CancellationSignalError, normalize_error
from .idempotency import IdempotencyStore, MemoryIdempotencyStore
from .models import ActionDefinition, ActionResult, CancellationSignal, ExecutionOptions, ExecutionTrace


async def _wait_for_cancellation(cancellation: CancellationSignal) -> bool:
    try:
        return await cancellation.wait()
    except BaseException as exc:
        raise CancellationSignalError(exc) from exc


async def _raise_if_cancelled(cancellation: CancellationSignal, message: str) -> None:
    if cancellation.is_set() and await _wait_for_cancellation(cancellation):
        raise ActionError("USER_CANCELLED", message)


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ActionError("CONTENT_MISMATCH", f"{name} must be a boolean.")
    return value


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
        self._page_locks: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()
        self._fallback_page_locks: WeakValueDictionary[int, asyncio.Lock] = (
            WeakValueDictionary()
        )
        for category in ACTION_CATEGORIES:
            setattr(self, category, ActionNamespace(self, category))

    def get_action_definition(self, action_id: str) -> ActionDefinition | None:
        return get_action_definition(action_id)

    def list_actions(self, category: str | None = None) -> list[ActionDefinition]:
        return list_actions(category)

    def _options(self, raw: Mapping[str, Any] | ExecutionOptions | None, payload: Mapping[str, Any]) -> ExecutionOptions:
        if isinstance(raw, ExecutionOptions):
            options = replace(raw, trace=ExecutionTrace())
        else:
            if raw is not None and not isinstance(raw, Mapping):
                raise ActionError("CONTENT_MISMATCH", "Execution options must be a mapping or ExecutionOptions.")
            value = raw or {}
            try:
                options = ExecutionOptions(
                    dry_run=_strict_bool(
                        value.get("dryRun", value.get("dry_run", payload.get("dryRun", False))),
                        "dryRun",
                    ),
                    confirm_live=_strict_bool(
                        value.get("confirmLive", value.get("confirm_live", payload.get("confirmLive", False))),
                        "confirmLive",
                    ),
                    idempotency_key=value.get("idempotencyKey", value.get("idempotency_key", payload.get("idempotencyKey"))),
                    timeout_ms=int(value.get("timeoutMs", value.get("timeout_ms", payload.get("timeoutMs", self.default_timeout_ms)))),
                    cancellation=value.get("cancellation"),
                    account_scope=value.get("accountScope", value.get("account_scope")),
                    capture_failure=_strict_bool(
                        value.get("captureFailure", value.get("capture_failure", False)),
                        "captureFailure",
                    ),
                    artifact_hook=value.get("artifactHook", value.get("artifact_hook")),
                )
            except ActionError:
                raise
            except (TypeError, ValueError) as error:
                raise ActionError("CONTENT_MISMATCH", "timeoutMs must be an integer.") from error
        options.dry_run = _strict_bool(options.dry_run, "dryRun")
        options.confirm_live = _strict_bool(options.confirm_live, "confirmLive")
        options.capture_failure = _strict_bool(options.capture_failure, "captureFailure")
        options.timeout_ms = max(250, min(int(options.timeout_ms), 120_000))
        if options.idempotency_key is not None:
            if not isinstance(options.idempotency_key, str) or not options.idempotency_key.strip():
                raise ActionError("CONTENT_MISMATCH", "idempotencyKey must be a non-empty string.")
            options.idempotency_key = options.idempotency_key.strip()
            if len(options.idempotency_key) > 200:
                raise ActionError("CONTENT_MISMATCH", "idempotencyKey must not exceed 200 characters.")
            if not isinstance(options.account_scope, str) or not options.account_scope.strip():
                raise ActionError("CONTENT_MISMATCH", "accountScope is required when idempotencyKey is provided.")
        if options.account_scope is not None:
            if not isinstance(options.account_scope, str) or not options.account_scope.strip():
                raise ActionError("CONTENT_MISMATCH", "accountScope must be a non-empty string.")
            options.account_scope = options.account_scope.strip()
            if len(options.account_scope) > 200:
                raise ActionError("CONTENT_MISMATCH", "accountScope must not exceed 200 characters.")
        if options.cancellation is not None and not (
            callable(getattr(options.cancellation, "is_set", None))
            and callable(getattr(options.cancellation, "wait", None))
        ):
            raise ActionError("CONTENT_MISMATCH", "cancellation must provide is_set() and async wait().")
        return options

    def _selected_tweet_id(self, page: Any) -> str | None:
        getter = getattr(type(self.adapter), "get_selected_tweet_id", None)
        if callable(getter):
            return cast(str | None, getter(self.adapter, page))
        selected = getattr(self.adapter, "selected_tweet_id", None)
        return str(selected) if selected else None

    def _validate_payload(self, page: Any, definition: ActionDefinition, payload: dict[str, Any]) -> None:
        selected_tweet_id = self._selected_tweet_id(page)
        if definition.requires_tweet and not payload.get("tweetId") and not selected_tweet_id:
            raise ActionError("TARGET_NOT_FOUND", f"Action {definition.id} requires tweetId or a selected post.")
        if definition.requires_comment and not payload.get("commentId"):
            raise ActionError("TARGET_NOT_FOUND", f"Action {definition.id} requires commentId.")
        for required in definition.input_schema.get("required", []):
            if required not in payload and not (required == "tweetId" and selected_tweet_id):
                raise ActionError("CONTENT_MISMATCH", f"Action {definition.id} requires payload.{required}.")
        for field in ("tweetId", "commentId", "replyId"):
            value = payload.get(field)
            if value is not None and (not isinstance(value, str) or not value.isdigit()):
                raise ActionError("CONTENT_MISMATCH", f"payload.{field} must be a string containing only digits.")

    def _idempotency_key(self, definition: ActionDefinition, options: ExecutionOptions) -> str | None:
        if definition.access != "write" or options.dry_run or not options.confirm_live or not options.idempotency_key:
            return None
        return f"{options.account_scope}:{definition.id}:{options.idempotency_key}"

    def _page_lock(self, page: Any) -> asyncio.Lock:
        try:
            lock = self._page_locks.get(page)
            if lock is None:
                lock = asyncio.Lock()
                self._page_locks[page] = lock
            return lock
        except TypeError:
            return self._fallback_page_locks.setdefault(id(page), asyncio.Lock())

    async def _dispatch_locked(
        self,
        page: Any,
        definition: ActionDefinition,
        payload: dict[str, Any],
        options: ExecutionOptions,
    ) -> dict[str, Any]:
        lock = self._page_lock(page)
        if options.cancellation is None:
            async with lock:
                options.trace.mark_dispatch_started()
                return await self.adapter.dispatch(page, definition.handler, payload, options)

        acquire_task = asyncio.create_task(lock.acquire())
        cancel_task = asyncio.create_task(_wait_for_cancellation(options.cancellation))
        acquired = False
        try:
            done, _ = await asyncio.wait({acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                cancel_requested = cancel_task.result()
                if cancel_requested and not acquire_task.done():
                    raise ActionError(
                        "USER_CANCELLED",
                        f"Action {definition.id} was cancelled while waiting for the Page.",
                    )
            acquired = await acquire_task
        except BaseException:
            if not acquire_task.done():
                acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
            elif not acquired and not acquire_task.cancelled() and acquire_task.exception() is None:
                lock.release()
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

        try:
            await _raise_if_cancelled(
                options.cancellation,
                f"Action {definition.id} was cancelled before execution.",
            )
            options.trace.mark_dispatch_started()
            return await self.adapter.dispatch(page, definition.handler, payload, options)
        finally:
            lock.release()

    def _uncertain_result(
        self,
        definition: ActionDefinition,
        execution: ExecutionOptions,
        started: float,
        error: ActionError,
    ) -> ActionResult:
        duration = round((asyncio.get_running_loop().time() - started) * 1000)
        return self._envelope(
            definition,
            {
                "status": "uncertain",
                "reason": (
                    "The live write may have been triggered, but its final state could not be confirmed. "
                    "Do not retry automatically."
                ),
                "error": error.to_dict(),
            },
            execution,
            duration,
        )

    async def execute(
        self,
        page: Any,
        action_id: str,
        payload: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | ExecutionOptions | None = None,
    ) -> ActionResult:
        definition = ACTION_DEFINITIONS.get(action_id)
        if not definition:
            raise ActionError("ACTION_UNSUPPORTED", f"Unsupported action: {action_id}", {"action": action_id})
        if not definition.enabled:
            raise ActionError("ACTION_UNSUPPORTED", definition.disabled_reason or f"Action {action_id} is disabled.")
        if payload is not None and not isinstance(payload, Mapping):
            raise ActionError("CONTENT_MISMATCH", "Action payload must be a mapping.")
        body = dict(payload or {})
        execution = self._options(options, body)
        if execution.cancellation:
            try:
                await _raise_if_cancelled(
                    execution.cancellation,
                    f"Action {action_id} was cancelled before execution.",
                )
            except CancellationSignalError as exc:
                raise exc.reason from exc
        if definition.access == "write" and not execution.dry_run and not execution.confirm_live:
            raise ActionError("CONFIRMATION_REQUIRED", f"Action {action_id} requires confirmLive=true.")
        self._validate_payload(page, definition, body)
        store_key = self._idempotency_key(definition, execution)
        if store_key:
            previous = await self.idempotency_store.claim(
                store_key,
                {"state": "pending", "action": action_id, "startedAt": datetime.now(UTC).isoformat()},
            )
            if previous is not None:
                previous_state = str(previous.get("state") or "")
                if previous_state == "pending":
                    raise ActionError(
                        "IDEMPOTENCY_IN_PROGRESS",
                        "Another execution currently owns this idempotency key.",
                        {"previous": previous},
                        retryable=True,
                    )
                if previous_state == "uncertain":
                    return self._envelope(
                        definition,
                        {
                            "status": "uncertain",
                            "reason": "The previous execution has an uncertain outcome. Do not retry automatically.",
                            "previous": previous,
                        },
                        execution,
                        0,
                    )
                if previous_state in {"success", "skipped"}:
                    return self._envelope(
                        definition,
                        {
                            "status": "skipped",
                            "reason": "idempotency-key-reused",
                            "previous": previous,
                        },
                        execution,
                        0,
                    )
                raise ActionError(
                    "IDEMPOTENCY_STATE_CONFLICT",
                    "The idempotency key contains a non-reusable terminal state.",
                    {"previous": previous},
                )

        started = asyncio.get_running_loop().time()
        try:
            raw = await asyncio.wait_for(
                self._dispatch_locked(page, definition, body, execution),
                timeout=execution.timeout_ms / 1000 + 0.5,
            )
            live_write = definition.access == "write" and execution.confirm_live and not execution.dry_run
            raw_status = str(raw.get("status", "success"))
            if live_write and raw_status in {"success", "uncertain"} and not execution.trace.mutation_triggered:
                raise ActionError(
                    "UNEXPECTED_ERROR",
                    "The adapter returned a live write result without marking mutation_triggered.",
                )
            if live_write and raw_status == "success":
                execution.trace.mark_postcondition_verified()
            duration = round((asyncio.get_running_loop().time() - started) * 1000)
            result = self._envelope(definition, raw, execution, duration)
        except TimeoutError as error:
            normalized = ActionError("TIMEOUT", f"Action {action_id} exceeded {execution.timeout_ms} ms.", retryable=definition.retry_policy == "safe")
            await self._capture(page, definition, normalized, execution)
            if (
                execution.trace.mutation_triggered
                and definition.access == "write"
                and execution.confirm_live
                and not execution.dry_run
            ):
                result = self._uncertain_result(definition, execution, started, normalized)
            else:
                if store_key:
                    await self.idempotency_store.delete(store_key)
                raise normalized from error
        except CancellationSignalError as error:
            if (
                execution.trace.mutation_triggered
                and definition.access == "write"
                and execution.confirm_live
                and not execution.dry_run
            ):
                # CancellationSignal.wait() may carry a domain-specific reason
                # such as TaskTimeoutError. Preserve that reason at the
                # uncertain boundary instead of relabelling every signal as a
                # user cancellation.
                normalized = normalize_error(error.reason)
                result = self._uncertain_result(definition, execution, started, normalized)
            else:
                if store_key:
                    await self.idempotency_store.delete(store_key)
                raise error.reason from error
        except BaseException as error:
            cancellation_origin = error.__cause__
            if (
                isinstance(cancellation_origin, CancellationSignalError)
                and cancellation_origin.reason is error
                and not (
                    execution.trace.mutation_triggered
                    and definition.access == "write"
                    and execution.confirm_live
                    and not execution.dry_run
                )
                ):
                if store_key:
                    await self.idempotency_store.delete(store_key)
                raise
            if isinstance(error, asyncio.CancelledError) or isinstance(
                cancellation_origin, CancellationSignalError
            ):
                normalized = ActionError("USER_CANCELLED", f"Action {action_id} was cancelled.")
            else:
                normalized = normalize_error(error)
            await self._capture(page, definition, normalized, execution)
            uncertain_write = (
                definition.access == "write"
                and execution.trace.mutation_triggered
                and execution.confirm_live
                and not execution.dry_run
                and (
                    isinstance(error, asyncio.CancelledError)
                    or normalized.uncertain
                    or normalized.code
                    in {"BROWSER_CLOSED_DURING_RUN", "PAGE_NAVIGATED", "TIMEOUT", "UNEXPECTED_ERROR"}
                )
            )
            if uncertain_write:
                result = self._uncertain_result(definition, execution, started, normalized)
            else:
                if store_key:
                    await self.idempotency_store.delete(store_key)
                raise normalized from error

        if store_key:
            try:
                await self.idempotency_store.put(
                    store_key,
                    {"state": result.status, "action": action_id, "completedAt": datetime.now(UTC).isoformat()},
                )
            except Exception:
                result.warnings.append("idempotency-result-persist-failed")
        return result

    async def _capture(self, page: Any, definition: ActionDefinition, error: ActionError, options: ExecutionOptions) -> None:
        if not options.capture_failure or not options.artifact_hook:
            return
        try:
            result = options.artifact_hook(page=page, action=definition.id, error=error.to_dict())
            if inspect.isawaitable(result):
                await result
        except Exception:
            return

    def _envelope(self, definition: ActionDefinition, raw: dict[str, Any], options: ExecutionOptions, duration_ms: int) -> ActionResult:
        status = raw.get("status", "success")
        if status not in {"success", "skipped", "navigating", "uncertain", "cancelled", "failed"}:
            raise ActionError("UNEXPECTED_ERROR", f"Adapter returned an invalid action status: {status!r}.")
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
                "executionTrace": {
                    "dispatchStarted": options.trace.dispatch_started,
                    "mutationTriggered": options.trace.mutation_triggered,
                    "postconditionVerified": options.trace.postcondition_verified,
                },
            },
        )
