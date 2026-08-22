from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel, Field

from .. import login_secrets
from ..task_sdk import TaskContext
from ._common import require_certain, result_data
from .spec import ProgramSpec

SPEC = ProgramSpec(
    name="login_accounts",
    version="1.0.0",
    title="批量登录 X 账号",
    description="按账户读取用户名、密码和可选 TOTP 密钥，低频逐步登录 X。",
    preserve_browser_on_uncertain=True,
    supports_batch_schedule=False,
)


class CredentialRef(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password_ref: str = Field(min_length=1, max_length=300)
    totp_secret_ref: str | None = Field(default=None, max_length=300)
    expected_username: str | None = Field(default=None, max_length=80)


class Params(BaseModel):
    credentials_by_account: dict[str, CredentialRef] = Field(default_factory=dict)
    max_concurrent_runs: int = Field(
        default=1,
        ge=1,
        le=20,
        title="同时登录账号数",
        description="同一批登录任务最多同时打开并操作多少个账号浏览器。",
    )
    delay_before_seconds_min: float = Field(default=0, ge=0, le=3600)
    delay_before_seconds_max: float = Field(default=0, ge=0, le=7200)
    step_delay_ms: int = Field(default=1800, ge=500, le=30000)
    typing_delay_ms: int = Field(default=85, ge=20, le=500)
    action_timeout_ms: int = Field(default=120000, ge=10000, le=120000)
    delete_credentials_on_success: bool = True


def _secret(reference: str, label: str) -> str:
    value = login_secrets.store().get(reference)
    if not value:
        raise RuntimeError(f"{label} 未配置或已删除")
    return value


async def run(context: TaskContext, params: Params) -> dict[str, Any]:
    await context.cancellation.raise_if_cancelled()
    account_id = context.account.account_id
    credential = params.credentials_by_account.get(account_id)
    if credential is None:
        raise RuntimeError(f"当前账户没有登录凭据配置: {account_id}")

    low = min(params.delay_before_seconds_min, params.delay_before_seconds_max)
    high = max(params.delay_before_seconds_min, params.delay_before_seconds_max)
    delay = random.uniform(low, high) if high > 0 else 0
    if delay:
        context.logger.info("登录前等待", delay_seconds=round(delay, 1))
        await context.cancellation.sleep(delay)

    password = _secret(credential.password_ref, "X 密码")
    totp_secret = (
        _secret(credential.totp_secret_ref, "X 2FA 密钥")
        if credential.totp_secret_ref
        else None
    )
    context.logger.info("开始登录 X", username=credential.username, has_totp=bool(totp_secret))
    result = await context.actions.account.login(
        {
            "username": credential.username,
            "password": password,
            "totpSecret": totp_secret,
            "expectedUsername": credential.expected_username or credential.username,
            "stepDelayMs": params.step_delay_ms,
            "typingDelayMs": params.typing_delay_ms,
        },
        options={
            "confirmLive": True,
            "timeoutMs": params.action_timeout_ms,
            "cancellation": context.cancellation,
        },
    )
    result = require_certain(result, task_run_id=context.cancellation.task_run_id)
    data = result_data(result)
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    logged_in = bool(session.get("loggedIn")) if isinstance(session, dict) else False
    if logged_in and params.delete_credentials_on_success:
        login_secrets.store().delete(credential.password_ref)
        if credential.totp_secret_ref:
            login_secrets.store().delete(credential.totp_secret_ref)
    context.logger.info(
        "X 登录流程完成",
        username=credential.username,
        status=getattr(result, "status", "success"),
        logged_in=logged_in,
    )
    return {
        "username": credential.username,
        "logged_in": logged_in,
        "session_username": session.get("username") if isinstance(session, dict) else None,
        "credentials_deleted": bool(logged_in and params.delete_credentials_on_success),
    }
