from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ..integrations.browser_custom import BrowserGateway, BrowserLease
from ..integrations.x_actions import BoundXActions
from ..models import (
    AccountRecord,
    CleanupReport,
    RunError,
    RunOutcome,
    RunStatus,
    TaskRunSnapshot,
)
from ..storage import AccountStore, TaskRunStore
from ..task_programs.registry import TaskProgram, TaskProgramRegistry
from ..task_sdk import (
    AccountContext,
    AIService,
    CancellationToken,
    CancellationTokenFactory,
    TaskCancelledError,
    TaskContext,
    TaskLogger,
    TaskLoggerFactory,
    TaskTimeoutError,
    TaskUncertainError,
)
from .concurrency import ExecutionSlotManager
from .locks import AccountLockManager


class BoundXActionsFactory(Protocol):
    def bind(self, page: Any) -> BoundXActions: ...


class TaskRunner:
    """Prepare, run, and clean up exactly one account's TaskRun."""

    def __init__(
        self,
        *,
        runner_id: str,
        run_store: TaskRunStore,
        account_store: AccountStore,
        program_registry: TaskProgramRegistry,
        account_locks: AccountLockManager,
        execution_slots: ExecutionSlotManager,
        browser_gateway: BrowserGateway,
        actions_factory: BoundXActionsFactory,
        ai_service: AIService,
        logger_factory: TaskLoggerFactory,
        cancellation_factory: CancellationTokenFactory | None = None,
        browser_acquire_timeout_seconds: float = 120.0,
    ) -> None:
        self.runner_id = runner_id
        self.run_store = run_store
        self.account_store = account_store
        self.program_registry = program_registry
        self.account_locks = account_locks
        self.execution_slots = execution_slots
        self.browser_gateway = browser_gateway
        self.actions_factory = actions_factory
        self.ai_service = ai_service
        self.logger_factory = logger_factory
        if browser_acquire_timeout_seconds <= 0:
            raise ValueError("browser acquire timeout must be positive")
        self.browser_acquire_timeout_seconds = browser_acquire_timeout_seconds
        self.cancellation_factory = cancellation_factory or CancellationTokenFactory(
            run_store.is_cancel_requested
        )

    async def execute(self, task_run_id: str) -> None:
        try:
            await self._execute(task_run_id)
        except asyncio.CancelledError:
            # asyncio.Task.cancel() is a hard runner interruption, not the
            # cooperative Task SDK cancellation path. Persist a terminal
            # state before preserving cancellation for the caller.
            await asyncio.shield(self._record_hard_cancel(task_run_id))
            raise

    async def _execute(self, task_run_id: str) -> None:
        run = await self.run_store.get_run(task_run_id)
        if run is None or run.status is not RunStatus.QUEUED:
            return
        if not await self.run_store.claim(run.id, self.runner_id):
            return
        run = await self.run_store.get_run(run.id) or run

        program = self.program_registry.get(run.program_name)
        logger = self._logger(run, program)
        logger.info("run_claimed")
        cancellation = self.cancellation_factory.create(task_run_id=run.id, deadline=run.deadline)

        try:
            await cancellation.raise_if_cancelled()
            program, params, account = await self._prepare(run, program, logger)
        except TaskCancelledError as exc:
            await self._finish(run, RunOutcome.cancelled(self._cancel_error(exc)), logger)
            return
        except TaskTimeoutError as exc:
            await self._finish(run, RunOutcome.failed(self._timeout_error(exc)), logger)
            return
        except _PreparationError as exc:
            await self._finish(run, RunOutcome.failed(exc.error), logger)
            return

        logger.info("account_lock_waiting")
        assert account.browser_account_id is not None
        try:
            account_lock = await cancellation.wait_for(
                self.account_locks.acquire(account.browser_account_id)
            )
        except TaskCancelledError as exc:
            await self._finish(run, RunOutcome.cancelled(self._cancel_error(exc)), logger)
            return
        except TaskTimeoutError as exc:
            await self._finish(run, RunOutcome.failed(self._timeout_error(exc)), logger)
            return

        async with account_lock:
            try:
                await cancellation.raise_if_cancelled()
                logger.info("account_lock_acquired")
                logger.info("execution_slot_waiting")
                slot = await cancellation.wait_for(self.execution_slots.acquire())
            except TaskCancelledError as exc:
                await self._finish(run, RunOutcome.cancelled(self._cancel_error(exc)), logger)
                return
            except TaskTimeoutError as exc:
                await self._finish(run, RunOutcome.failed(self._timeout_error(exc)), logger)
                return

            async with slot:
                logger.info("execution_slot_acquired")
                await self._execute_in_slot(run, program, params, account, cancellation, logger)

    async def _record_hard_cancel(self, task_run_id: str) -> None:
        run = await self.run_store.get_run(task_run_id)
        if (
            run is None
            or run.status.terminal
            or run.claimed_by != self.runner_id
        ):
            return
        program = self.program_registry.get(run.program_name)
        logger = self._logger(run, program)
        if run.status is RunStatus.RUNNING:
            outcome = RunOutcome.uncertain(self._interrupted_error())
        else:
            outcome = RunOutcome.cancelled(
                self._cancel_error(TaskCancelledError(run.id))
            )
        await self._finish(run, outcome, logger)

    async def _prepare(
        self,
        run: TaskRunSnapshot,
        program: TaskProgram | None,
        logger: TaskLogger,
    ) -> tuple[TaskProgram, BaseModel, AccountRecord]:
        if program is None:
            raise _PreparationError(
                RunError("PROGRAM_NOT_FOUND", f"Task program {run.program_name!r} is not deployed")
            )
        logger.info("program_resolved", program_version=program.SPEC.version)
        if run.requested_program_version and run.requested_program_version != program.SPEC.version:
            raise _PreparationError(
                RunError(
                    "PROGRAM_VERSION_UNAVAILABLE",
                    f"Requested {run.requested_program_version}, deployed {program.SPEC.version}",
                )
            )
        try:
            params = program.Params.model_validate(dict(run.params))
        except ValidationError as exc:
            details = {
                "errors": [
                    {"path": list(item["loc"]), "message": item["msg"], "type": item["type"]}
                    for item in exc.errors(include_input=False, include_url=False)
                ]
            }
            raise _PreparationError(
                RunError("INVALID_TASK_PARAMS", "Task parameters did not validate", details=details)
            ) from exc
        logger.info("params_validated")
        account = await self.account_store.get_account(run.account_id)
        if account is None:
            raise _PreparationError(RunError("ACCOUNT_NOT_FOUND", "Task account does not exist"))
        if account.archived or not account.enabled:
            raise _PreparationError(RunError("ACCOUNT_DISABLED", "Task account is disabled"))
        if not account.browser_account_id:
            raise _PreparationError(
                RunError("BROWSER_ACCOUNT_NOT_BOUND", "Task account has no browser-custom account")
            )
        return program, params, account

    async def _execute_in_slot(
        self,
        run: TaskRunSnapshot,
        program: TaskProgram,
        params: BaseModel,
        account: AccountRecord,
        cancellation: CancellationToken,
        logger: TaskLogger,
    ) -> None:
        lease: BrowserLease | None = None
        outcome: RunOutcome | None = None
        cleanup = CleanupReport()
        try:
            await cancellation.raise_if_cancelled()
            logger.info("browser_acquire_started")
            browser_account_id = account.browser_account_id
            if browser_account_id is None:
                raise _PreparationError(
                    RunError(
                        "BROWSER_ACCOUNT_NOT_BOUND",
                        "Task account has no browser-custom account",
                    )
                )
            try:
                lease = await cancellation.wait_for(
                    asyncio.wait_for(
                        self.browser_gateway.acquire(
                            browser_account_id=browser_account_id,
                            task_run_id=run.id,
                        ),
                        timeout=self.browser_acquire_timeout_seconds,
                    )
                )
            except (TaskCancelledError, TaskTimeoutError):
                raise
            except TimeoutError as exc:
                raise _PreparationError(
                    RunError(
                        "BROWSER_START_TIMEOUT",
                        "Timed out while preparing the account browser",
                        source="browser-custom",
                        retryable=True,
                    )
                ) from exc
            except Exception as exc:
                raise _PreparationError(
                    RunError(
                        "BROWSER_START_FAILED",
                        str(exc) or "Could not acquire account browser",
                        source="browser-custom",
                        retryable=True,
                        exception_type=type(exc).__name__,
                    )
                ) from exc
            logger.info("browser_acquired", browser_was_started=lease.browser_was_started)
            actions = self.actions_factory.bind(lease.page)
            context = TaskContext(
                account=AccountContext.from_record(account),
                actions=actions,
                ai=self.ai_service,
                logger=logger,
                cancellation=cancellation,
            )
            logger.info("context_created")
            await cancellation.raise_if_cancelled()
            if not await self.run_store.mark_running(run.id, self.runner_id, program.SPEC.version):
                if await self.run_store.is_cancel_requested(run.id):
                    raise TaskCancelledError(run.id)
                raise _PreparationError(
                    RunError(
                        "RUN_STATE_CONFLICT",
                        "TaskRun could not transition from queued to running",
                        source="task-runner",
                    )
                )
            logger.info("program_started")
            outcome = await self._invoke(program, context, params)
            logger.info("program_finished", status=outcome.status.value)
        except _PreparationError as exc:
            outcome = RunOutcome.failed(exc.error)
        except TaskCancelledError as exc:
            outcome = RunOutcome.cancelled(self._cancel_error(exc))
        except TaskTimeoutError as exc:
            outcome = RunOutcome.failed(self._timeout_error(exc))
        except TaskUncertainError as exc:
            outcome = RunOutcome.uncertain(self._uncertain_error(exc))
        except Exception as exc:
            outcome = self._exception_outcome(exc)
        finally:
            if lease is not None:
                logger.info("cleanup_started")
                cleanup = await self._safe_cleanup(
                    lease,
                    close_browser=run.browser_end_policy.value == "close",
                )
                logger.info("cleanup_finished", warnings=list(cleanup.warnings))
        if outcome is not None:
            await self._finish(run, outcome, logger, cleanup)

    async def _invoke(self, program: TaskProgram, context: TaskContext, params: Any) -> RunOutcome:
        try:
            output = await program.run(context, params)
            if not isinstance(output, Mapping):
                raise TypeError("Task Program output must be a JSON object")
            return RunOutcome.succeeded(dict(output))
        except TaskCancelledError as exc:
            return RunOutcome.cancelled(self._cancel_error(exc))
        except TaskTimeoutError as exc:
            return RunOutcome.failed(self._timeout_error(exc))
        except TaskUncertainError as exc:
            return RunOutcome.uncertain(self._uncertain_error(exc))
        except Exception as exc:
            return self._exception_outcome(exc)

    def _exception_outcome(self, exc: Exception) -> RunOutcome:
        if getattr(exc, "code", None) == "USER_CANCELLED":
            return RunOutcome.cancelled(
                RunError(
                    "TASK_CANCELLED",
                    str(exc),
                    source="x-actions-playwright",
                    exception_type=type(exc).__name__,
                )
            )
        if bool(getattr(exc, "uncertain", False)):
            return RunOutcome.uncertain(
                RunError(
                    getattr(exc, "code", "ACTION_STATE_UNKNOWN"),
                    str(exc),
                    source="x-actions-playwright",
                    details=getattr(exc, "details", {}) or {},
                    exception_type=type(exc).__name__,
                )
            )
        return RunOutcome.failed(
            RunError(
                getattr(exc, "code", "UNHANDLED_TASK_ERROR"),
                str(exc) or type(exc).__name__,
                source="task-program",
                retryable=bool(getattr(exc, "retryable", False)),
                details=getattr(exc, "details", {}) or {},
                exception_type=type(exc).__name__,
            )
        )

    async def _safe_cleanup(self, lease: BrowserLease, *, close_browser: bool) -> CleanupReport:
        try:
            return await lease.release(close_browser=close_browser)
        except Exception as exc:
            return CleanupReport(
                ({"code": "BROWSER_LEASE_RELEASE_FAILED", "message": str(exc)},)
            )

    async def _finish(
        self,
        run: TaskRunSnapshot,
        outcome: RunOutcome,
        logger: TaskLogger,
        cleanup: CleanupReport | None = None,
    ) -> None:
        saved = await self.run_store.finish(
            run.id, self.runner_id, outcome, cleanup or CleanupReport()
        )
        if saved:
            logger.info("run_finished", status=outcome.status.value)

    def _logger(self, run: TaskRunSnapshot, program: TaskProgram | None) -> TaskLogger:
        return self.logger_factory.create(
            task_run_id=run.id,
            task_id=run.task_id,
            program_name=run.program_name,
            program_version=program.SPEC.version if program else "unknown",
            account_id=run.account_id,
            runner_id=self.runner_id,
        )

    @staticmethod
    def _cancel_error(exc: TaskCancelledError) -> RunError:
        return RunError("TASK_CANCELLED", str(exc))

    @staticmethod
    def _timeout_error(exc: TaskTimeoutError) -> RunError:
        return RunError("TASK_TIMEOUT", str(exc))

    @staticmethod
    def _uncertain_error(exc: TaskUncertainError) -> RunError:
        return RunError(
            "ACTION_STATE_UNKNOWN",
            str(exc),
            source="task-program",
            details={"action_id": exc.action_id, **exc.details},
        )

    @staticmethod
    def _interrupted_error() -> RunError:
        return RunError(
            "RUNNER_INTERRUPTED",
            "Runner execution was forcibly interrupted after the task started; "
            "some business actions may have completed. Verify before rerunning.",
            source="task-runner",
            retryable=False,
        )


class _PreparationError(RuntimeError):
    def __init__(self, error: RunError) -> None:
        super().__init__(error.message)
        self.error = error
