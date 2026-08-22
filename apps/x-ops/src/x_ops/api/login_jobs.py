from __future__ import annotations

import math
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from x_ops import login_secrets

from .contracts import AdminAPIError, AdminServices, JsonObject
from .dependencies import get_services

router = APIRouter(tags=["批量登录"])


class LoginCredential(BaseModel):
    account_id: str = Field(min_length=1, max_length=160)
    browser_name: str | None = Field(default=None, max_length=200)
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=4000)
    totp_secret: str | None = Field(default=None, max_length=4000)
    expected_username: str | None = Field(default=None, max_length=80)


class LoginJobCreate(BaseModel):
    name: str = Field(default="批量登录 X 账号", min_length=1, max_length=160)
    credentials: list[LoginCredential] = Field(min_length=1, max_length=500)
    max_concurrent_runs: int = Field(default=1, ge=1, le=20)
    delay_before_seconds_min: float = Field(default=0, ge=0, le=3600)
    delay_before_seconds_max: float = Field(default=0, ge=0, le=7200)
    step_delay_ms: int = Field(default=1800, ge=500, le=30000)
    typing_delay_ms: int = Field(default=85, ge=20, le=500)
    action_timeout_ms: int = Field(default=120000, ge=10000, le=120000)
    delete_credentials_on_success: bool = True
    run_now: bool = True

    @field_validator("credentials")
    @classmethod
    def unique_accounts(cls, value: list[LoginCredential]) -> list[LoginCredential]:
        seen: set[str] = set()
        for item in value:
            if item.account_id in seen:
                raise ValueError(f"账号重复: {item.account_id}")
            seen.add(item.account_id)
        return value


@router.post("/login-jobs")
async def create_login_job(
    body: LoginJobCreate,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    known = {str(item["id"]) for item in await services.backend.list_accounts()}
    missing = [item.account_id for item in body.credentials if item.account_id not in known]
    if missing:
        raise AdminAPIError(400, f"未知账户: {', '.join(missing)}")

    job_id = uuid.uuid4().hex[:12]
    waves = math.ceil(len(body.credentials) / body.max_concurrent_runs)
    estimated_wave_seconds = (
        max(body.delay_before_seconds_min, body.delay_before_seconds_max)
        + body.action_timeout_ms / 1000
        + 60
    )
    runtime_timeout = int(
        services.runtime_settings.get().get("default_task_timeout_seconds", 3600)
    )
    task_timeout_seconds = max(runtime_timeout, math.ceil(waves * estimated_wave_seconds))
    secret_store = login_secrets.store()
    credentials_by_account: dict[str, JsonObject] = {}
    written_refs: list[str] = []
    try:
        for item in body.credentials:
            password_ref = login_secrets.reference("password", job_id, item.account_id)
            secret_store.set(password_ref, item.password)
            written_refs.append(password_ref)
            payload: JsonObject = {
                "username": item.username,
                "password_ref": password_ref,
                "expected_username": item.expected_username or item.username,
            }
            if item.totp_secret:
                totp_ref = login_secrets.reference("totp", job_id, item.account_id)
                secret_store.set(totp_ref, item.totp_secret)
                written_refs.append(totp_ref)
                payload["totp_secret_ref"] = totp_ref
            credentials_by_account[item.account_id] = payload

        task = await services.backend.create_task(
            {
                "name": body.name,
                "description": "通过批量登录入口创建；敏感字段已写入 secret store，任务参数只保存引用。",
                "program_name": "login_accounts",
                "account_ids": [item.account_id for item in body.credentials],
                "enabled": body.run_now,
                "browser_end_policy": "close",
                "task_timeout_seconds": task_timeout_seconds,
                "params": {
                    "credentials_by_account": credentials_by_account,
                    "max_concurrent_runs": body.max_concurrent_runs,
                    "delay_before_seconds_min": body.delay_before_seconds_min,
                    "delay_before_seconds_max": body.delay_before_seconds_max,
                    "step_delay_ms": body.step_delay_ms,
                    "typing_delay_ms": body.typing_delay_ms,
                    "action_timeout_ms": body.action_timeout_ms,
                    "delete_credentials_on_success": body.delete_credentials_on_success,
                },
            }
        )
    except Exception:
        for reference in written_refs:
            secret_store.delete(reference)
        raise

    run_result = None
    if body.run_now:
        run_result = await services.backend.trigger_task(
            str(task["id"]), "login-job", fire_key=job_id
        )
    return {"task": task, "run": run_result, "credential_count": len(body.credentials)}
