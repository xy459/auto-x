from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .models import (
    AccountRecord,
    BrowserEndPolicy,
    CleanupReport,
    RunOutcome,
    RunStatus,
    Task,
    TaskLogRecord,
    TaskRunSnapshot,
    utc_now,
)


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str | None, default: Any = None) -> Any:
    return default if value is None else json.loads(value)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


class TaskRunStore(Protocol):
    async def get_run(self, run_id: str) -> TaskRunSnapshot | None: ...
    async def claim(self, run_id: str, runner_id: str) -> bool: ...
    async def is_cancel_requested(self, run_id: str) -> bool: ...
    async def mark_running(self, run_id: str, runner_id: str, program_version: str) -> bool: ...
    async def finish(self, run_id: str, runner_id: str, outcome: RunOutcome, cleanup: CleanupReport) -> bool: ...


class AccountStore(Protocol):
    async def get_account(self, account_id: str) -> AccountRecord | None: ...
    async def list_accounts(self) -> list[AccountRecord]: ...
    async def create_account(self, account: AccountRecord) -> AccountRecord: ...
    async def update_account(self, account_id: str, **changes: Any) -> AccountRecord | None: ...


def _ensure_unique_active_browser_account(
    accounts: Iterable[AccountRecord], candidate: AccountRecord
) -> None:
    if not candidate.browser_account_id or not candidate.enabled or candidate.archived:
        return
    for account in accounts:
        if (
            account.id != candidate.id
            and account.enabled
            and not account.archived
            and account.browser_account_id == candidate.browser_account_id
        ):
            raise ValueError(
                f"browser_account_id already belongs to active account {account.id!r}: "
                f"{candidate.browser_account_id}"
            )


class InMemoryAccountStore:
    def __init__(self, accounts: Iterable[AccountRecord] = ()) -> None:
        self._accounts = {account.id: account for account in accounts}

    async def get_account(self, account_id: str) -> AccountRecord | None:
        return self._accounts.get(account_id)

    async def list_accounts(self) -> list[AccountRecord]:
        return sorted(self._accounts.values(), key=lambda account: account.id)

    def put(self, account: AccountRecord) -> None:
        self._accounts[account.id] = account

    async def create_account(self, account: AccountRecord) -> AccountRecord:
        if account.id in self._accounts:
            raise ValueError(f"account already exists: {account.id}")
        _ensure_unique_active_browser_account(self._accounts.values(), account)
        self.put(account)
        return account

    async def update_account(self, account_id: str, **changes: Any) -> AccountRecord | None:
        account = self._accounts.get(account_id)
        if account is None:
            return None
        updated = replace(account, **changes)
        _ensure_unique_active_browser_account(self._accounts.values(), updated)
        self.put(updated)
        return updated


class JsonAccountStore:
    """Atomic JSON persistence for non-sensitive X business account metadata."""

    def __init__(self, path: str | Path | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "data" / "accounts.json"
        configured = path if path is not None else os.environ.get("X_OPS_ACCOUNTS_PATH")
        self.path = Path(configured or default).expanduser().resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, AccountRecord]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        values = payload.get("accounts", payload) if isinstance(payload, dict) else payload
        result: dict[str, AccountRecord] = {}
        for item in values or []:
            account = AccountRecord(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                browser_account_id=item.get("browser_account_id"),
                username=item.get("username"),
                tags=tuple(item.get("tags") or ()),
                metadata=dict(item.get("metadata") or {}),
                enabled=bool(item.get("enabled", True)),
                archived=bool(item.get("archived", False)),
            )
            result[account.id] = account
        return result

    def _write(self, accounts: dict[str, AccountRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "accounts": [
                {
                    "id": account.id,
                    "name": account.name,
                    "browser_account_id": account.browser_account_id,
                    "username": account.username,
                    "tags": list(account.tags),
                    "metadata": dict(account.metadata),
                    "enabled": account.enabled,
                    "archived": account.archived,
                }
                for account in sorted(accounts.values(), key=lambda value: value.id)
            ]
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".accounts-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    async def get_account(self, account_id: str) -> AccountRecord | None:
        with self._lock:
            return self._read().get(account_id)

    async def list_accounts(self) -> list[AccountRecord]:
        with self._lock:
            return sorted(self._read().values(), key=lambda account: account.id)

    async def create_account(self, account: AccountRecord) -> AccountRecord:
        with self._lock:
            accounts = self._read()
            if account.id in accounts:
                raise ValueError(f"account already exists: {account.id}")
            _ensure_unique_active_browser_account(accounts.values(), account)
            accounts[account.id] = account
            self._write(accounts)
            return account

    async def update_account(self, account_id: str, **changes: Any) -> AccountRecord | None:
        with self._lock:
            accounts = self._read()
            account = accounts.get(account_id)
            if account is None:
                return None
            updated = replace(account, **changes)
            _ensure_unique_active_browser_account(accounts.values(), updated)
            accounts[account_id] = updated
            self._write(accounts)
            return updated


class SQLiteStore:
    """Small SQLite persistence for tasks, task_runs, and task_logs only."""

    # Known v1 limitation: sqlite3 calls block the event loop; migrate to aiosqlite or asyncio.to_thread.

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")

    async def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    program_name TEXT NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES tasks(id),
                    trigger_id TEXT,
                    rerun_of TEXT REFERENCES task_runs(id),
                    program_name TEXT NOT NULL,
                    requested_program_version TEXT,
                    program_version TEXT,
                    account_id TEXT NOT NULL,
                    params_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','running','succeeded','failed','uncertain','cancelled'
                    )),
                    browser_end_policy TEXT NOT NULL CHECK(browser_end_policy IN ('keep_open','close')),
                    deadline TEXT,
                    output_json TEXT,
                    error_json TEXT,
                    cleanup_warnings_json TEXT,
                    cancel_requested_at TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_runs_status_created
                    ON task_runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_runs_account_status
                    ON task_runs(account_id, status);

                CREATE TABLE IF NOT EXISTS scheduled_fires (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    fire_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, fire_key)
                );

                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_run_id TEXT NOT NULL REFERENCES task_runs(id),
                    account_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_logs_run_id
                    ON task_logs(task_run_id, id);
                """
            )

    async def close(self) -> None:
        with self._lock:
            self._connection.close()

    async def create_task(self, task: Task) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.name,
                    task.program_name,
                    _dump(list(task.account_ids)),
                    _dump(dict(task.params)),
                    int(task.enabled),
                    _iso(task.created_at),
                    _iso(task.updated_at),
                ),
            )

    async def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    async def list_tasks(self, *, enabled: bool | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        args: tuple[Any, ...] = ()
        if enabled is not None:
            sql += " WHERE enabled = ?"
            args = (int(enabled),)
        sql += " ORDER BY created_at, id"
        with self._lock:
            rows = self._connection.execute(sql, args).fetchall()
        return [self._task_from_row(row) for row in rows]

    def _task_from_row(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            name=row["name"],
            program_name=row["program_name"],
            account_ids=tuple(_load(row["account_ids_json"], [])),
            params=_load(row["params_json"], {}),
            enabled=bool(row["enabled"]),
            created_at=_dt(row["created_at"]) or utc_now(),
            updated_at=_dt(row["updated_at"]) or utc_now(),
        )

    async def update_task(self, task: Task) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET name = ?, program_name = ?, account_ids_json = ?, params_json = ?,
                    enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    task.name,
                    task.program_name,
                    _dump(list(task.account_ids)),
                    _dump(dict(task.params)),
                    int(task.enabled),
                    _iso(task.updated_at),
                    task.id,
                ),
            )
            return cursor.rowcount == 1

    async def trigger_task(
        self,
        task_id: str,
        *,
        trigger_id: str | None = None,
        requested_program_version: str | None = None,
        browser_end_policy: BrowserEndPolicy = BrowserEndPolicy.KEEP_OPEN,
        deadline: datetime | None = None,
        fire_key: str | None = None,
    ) -> list[TaskRunSnapshot]:
        group = trigger_id or str(uuid.uuid4())
        created = utc_now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                task = self._task_from_row(row)
                if not task.enabled:
                    raise ValueError("cannot trigger a disabled task")
                runs = [
                    TaskRunSnapshot(
                        id=str(uuid.uuid4()),
                        task_id=task.id,
                        trigger_id=group,
                        program_name=task.program_name,
                        requested_program_version=requested_program_version,
                        account_id=account_id,
                        params=dict(task.params),
                        status=RunStatus.QUEUED,
                        browser_end_policy=browser_end_policy,
                        deadline=deadline,
                        created_at=created,
                    )
                    for account_id in task.account_ids
                ]
                if fire_key is not None:
                    reserved = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO scheduled_fires (task_id, fire_key, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (task.id, fire_key, _iso(created)),
                    )
                    if reserved.rowcount == 0:
                        self._connection.execute("COMMIT")
                        return []
                for run in runs:
                    self._insert_run(run)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return runs

    async def create_run(self, run: TaskRunSnapshot) -> None:
        if run.status is not RunStatus.QUEUED:
            raise ValueError("new TaskRun must be queued")
        with self._lock:
            self._insert_run(run)

    def _insert_run(self, run: TaskRunSnapshot) -> None:
        self._connection.execute(
                """
                INSERT INTO task_runs (
                    id, task_id, trigger_id, rerun_of, program_name,
                    requested_program_version, program_version, account_id,
                    params_snapshot_json, status, browser_end_policy, deadline,
                    output_json, error_json, cleanup_warnings_json,
                    cancel_requested_at, claimed_by, claimed_at, started_at,
                    finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.task_id,
                    run.trigger_id,
                    run.rerun_of,
                    run.program_name,
                    run.requested_program_version,
                    run.program_version,
                    run.account_id,
                    _dump(dict(run.params)),
                    run.status.value,
                    run.browser_end_policy.value,
                    _iso(run.deadline) if run.deadline else None,
                    _dump(run.output),
                    _dump(run.error),
                    _dump(list(run.cleanup_warnings)),
                    _iso(run.cancel_requested_at) if run.cancel_requested_at else None,
                    run.claimed_by,
                    _iso(run.claimed_at) if run.claimed_at else None,
                    _iso(run.started_at) if run.started_at else None,
                    _iso(run.finished_at) if run.finished_at else None,
                    _iso(run.created_at),
                ),
        )

    async def list_runs(
        self,
        *,
        task_id: str | None = None,
        account_id: str | None = None,
        status: RunStatus | None = None,
    ) -> list[TaskRunSnapshot]:
        clauses: list[str] = []
        args: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            args.append(task_id)
        if account_id is not None:
            clauses.append("account_id = ?")
            args.append(account_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status.value)
        sql = "SELECT * FROM task_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        with self._lock:
            rows = self._connection.execute(sql, args).fetchall()
        return [self._run_from_row(row) for row in rows]

    async def create_rerun(self, run_id: str, *, new_run_id: str | None = None) -> TaskRunSnapshot:
        old = await self.get_run(run_id)
        if old is None:
            raise KeyError(run_id)
        rerun = TaskRunSnapshot(
            id=new_run_id or str(uuid.uuid4()),
            task_id=old.task_id,
            trigger_id=f"rerun:{uuid.uuid4()}",
            rerun_of=old.id,
            program_name=old.program_name,
            requested_program_version=old.program_version or old.requested_program_version,
            account_id=old.account_id,
            params=dict(old.params),
            status=RunStatus.QUEUED,
            browser_end_policy=old.browser_end_policy,
            deadline=None,
            created_at=utc_now(),
        )
        await self.create_run(rerun)
        return rerun

    async def get_run(self, run_id: str) -> TaskRunSnapshot | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row else None

    def _run_from_row(self, row: sqlite3.Row) -> TaskRunSnapshot:
        return TaskRunSnapshot(
            id=row["id"],
            task_id=row["task_id"],
            trigger_id=row["trigger_id"],
            rerun_of=row["rerun_of"],
            program_name=row["program_name"],
            requested_program_version=row["requested_program_version"],
            program_version=row["program_version"],
            account_id=row["account_id"],
            params=_load(row["params_snapshot_json"], {}),
            status=RunStatus(row["status"]),
            browser_end_policy=BrowserEndPolicy(row["browser_end_policy"]),
            deadline=_dt(row["deadline"]),
            output=_load(row["output_json"]),
            error=_load(row["error_json"]),
            cleanup_warnings=tuple(_load(row["cleanup_warnings_json"], [])),
            cancel_requested_at=_dt(row["cancel_requested_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=_dt(row["claimed_at"]),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            created_at=_dt(row["created_at"]) or utc_now(),
        )

    async def claim(self, run_id: str, runner_id: str) -> bool:
        now = _iso()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE task_runs
                SET claimed_by = ?, claimed_at = ?
                WHERE id = ? AND status = 'queued' AND claimed_by IS NULL
                """,
                (runner_id, now, run_id),
            )
            return cursor.rowcount == 1

    async def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT cancel_requested_at FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return bool(row and row[0])

    async def request_cancel(self, run_id: str) -> bool:
        now = _iso()
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not row or RunStatus(row[0]).terminal:
                return False
            if row[0] == RunStatus.QUEUED.value:
                cursor = self._connection.execute(
                    """
                    UPDATE task_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        status = 'cancelled', finished_at = ?
                    WHERE id = ? AND status = 'queued' AND claimed_by IS NULL
                    """,
                    (now, now, run_id),
                )
                if cursor.rowcount == 1:
                    return True
            cursor = self._connection.execute(
                """
                UPDATE task_runs SET cancel_requested_at = COALESCE(cancel_requested_at, ?)
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (now, run_id),
            )
            return cursor.rowcount == 1

    async def mark_running(self, run_id: str, runner_id: str, program_version: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE task_runs
                SET status = 'running', program_version = ?, started_at = ?
                WHERE id = ? AND status = 'queued' AND claimed_by = ?
                  AND cancel_requested_at IS NULL
                """,
                (program_version, _iso(), run_id, runner_id),
            )
            return cursor.rowcount == 1

    async def finish(
        self,
        run_id: str,
        runner_id: str,
        outcome: RunOutcome,
        cleanup: CleanupReport | None = None,
    ) -> bool:
        if not outcome.status.terminal:
            raise ValueError("finish requires a terminal status")
        cleanup = cleanup or CleanupReport()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE task_runs
                SET status = ?, output_json = ?, error_json = ?,
                    cleanup_warnings_json = ?, finished_at = ?
                WHERE id = ? AND claimed_by = ? AND status IN ('queued', 'running')
                """,
                (
                    outcome.status.value,
                    _dump(dict(outcome.output)) if outcome.output is not None else None,
                    _dump(outcome.error.to_dict()) if outcome.error else None,
                    _dump(list(cleanup.warnings)),
                    _iso(),
                    run_id,
                    runner_id,
                ),
            )
            return cursor.rowcount == 1

    def append_log_now(
        self,
        *,
        task_run_id: str,
        account_id: str,
        level: str,
        message: str,
        fields: dict[str, Any],
        created_at: datetime | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO task_logs(task_run_id, account_id, level, message, fields_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_run_id, account_id, level, message, _dump(fields), _iso(created_at)),
            )

    async def list_logs(self, run_id: str) -> list[TaskLogRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_logs WHERE task_run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            TaskLogRecord(
                id=row["id"],
                task_run_id=row["task_run_id"],
                account_id=row["account_id"],
                level=row["level"],
                message=row["message"],
                fields=_load(row["fields_json"], {}),
                created_at=_dt(row["created_at"]) or utc_now(),
            )
            for row in rows
        ]

    async def delete_logs_before(self, cutoff: datetime) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM task_logs WHERE created_at < ?", (_iso(cutoff),)
            )
            return cursor.rowcount

    async def recover_interrupted(self) -> int:
        error = _dump(
            {
                "code": "RUNNER_INTERRUPTED",
                "message": "任务执行进程异常终止，部分业务动作可能已经完成，请检查后再决定是否重新运行。",
                "source": "task-runner",
                "retryable": False,
                "details": {},
            }
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now = _iso()
                cursor = self._connection.execute(
                    """
                    UPDATE task_runs
                    SET status = 'uncertain', error_json = ?, finished_at = ?
                    WHERE status = 'running'
                    """,
                    (error, now),
                )
                interrupted = cursor.rowcount
                self._connection.execute(
                    """
                    UPDATE task_runs
                    SET status = 'cancelled', finished_at = ?
                    WHERE status = 'queued' AND cancel_requested_at IS NOT NULL
                    """,
                    (now,),
                )
                self._connection.execute(
                    """
                    UPDATE task_runs
                    SET claimed_by = NULL, claimed_at = NULL
                    WHERE status = 'queued'
                    """
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
            return interrupted
