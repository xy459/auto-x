from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from .errors import ActionError
from .models import ActionResult, WorkflowStep

Condition = Callable[[dict[str, Any]], bool | Awaitable[bool]]


class WorkflowRunner:
    def __init__(self, actions: Any) -> None:
        self.actions = actions

    async def run(
        self,
        page: Any,
        steps: list[WorkflowStep | dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
        cancellation: asyncio.Event | None = None,
        on_step: Callable[[dict[str, Any]], Any] | None = None,
    ) -> ActionResult:
        if not steps:
            raise ActionError("CONTENT_MISMATCH", "Workflow steps must be a non-empty list.")
        started = asyncio.get_running_loop().time()
        results: list[dict[str, Any]] = []
        state = context or {}

        for index, raw_step in enumerate(steps):
            if cancellation and cancellation.is_set():
                raise ActionError("USER_CANCELLED", f"Workflow cancelled before step {index + 1}.")
            step = raw_step if isinstance(raw_step, WorkflowStep) else WorkflowStep(**raw_step)
            if step.when is not None:
                allowed = step.when({"results": results, "context": state, "actions": self.actions})
                if hasattr(allowed, "__await__"):
                    allowed = await allowed
                if not allowed:
                    results.append({"index": index, "status": "skipped", "action": step.action, "category": "workflow", "data": {"reason": "workflow-condition-false"}, "evidence": [], "warnings": [], "meta": {}})
                    continue
            definition = self.actions.get_action_definition(step.action)
            if not definition:
                raise ActionError("ACTION_UNSUPPORTED", f"Unsupported workflow action: {step.action}")
            retries = max(0, min(int(step.retries), 3))
            if retries and definition.retry_policy != "safe":
                raise ActionError("TARGET_UNSAFE", f"Automatic retries are disabled for {step.action}.")
            result: ActionResult | None = None
            last_error: ActionError | None = None
            for attempt in range(retries + 1):
                try:
                    options = {**step.options, "cancellation": cancellation}
                    result = await self.actions.execute(page, step.action, step.payload, options)
                    break
                except ActionError as error:
                    last_error = error
                    if attempt >= retries or not error.retryable:
                        raise
                    await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
            if result is None:
                raise last_error or ActionError("UNEXPECTED_ERROR", "Workflow step returned no result.")
            item = {"index": index, **result.to_dict()}
            results.append(item)
            if on_step:
                callback_result = on_step({"index": index, "step": asdict(step), "result": item, "results": results, "context": state})
                if hasattr(callback_result, "__await__"):
                    await callback_result
            if step.delay_after_ms:
                delay = min(max(step.delay_after_ms, 0), 60_000) / 1000
                if cancellation:
                    try:
                        await asyncio.wait_for(cancellation.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(delay)
                if cancellation and cancellation.is_set():
                    raise ActionError("USER_CANCELLED", f"Workflow cancelled after step {index + 1}.")
            if result.status == "uncertain" and not step.continue_on_uncertain:
                break
            if result.status == "navigating" and not step.continue_on_navigating:
                break

        status = "uncertain" if any(item["status"] == "uncertain" for item in results) else "navigating" if any(item["status"] == "navigating" for item in results) else "success"
        return ActionResult(
            status=status,
            action="workflow.run",
            category="workflow",
            data={"steps": results, "context": state},
            meta={"durationMs": round((asyncio.get_running_loop().time() - started) * 1000)},
        )


__all__ = ["WorkflowRunner", "WorkflowStep"]
