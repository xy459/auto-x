"""Request models for the management API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    browser_account_id: str = Field(min_length=1, max_length=160)
    username: str | None = Field(default=None, max_length=80)
    x_user_id: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2000)
    enabled: bool = True


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    browser_account_id: str | None = Field(default=None, min_length=1, max_length=160)
    username: str | None = Field(default=None, max_length=80)
    x_user_id: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    note: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class BrowserBatchRequest(BaseModel):
    action: Literal["open", "close", "restart", "refresh"]
    account_ids: list[str] = Field(min_length=1)

    @field_validator("account_ids")
    @classmethod
    def unique_accounts(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    program_name: str = Field(min_length=1, max_length=160)
    account_ids: list[str] = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    browser_end_policy: Literal["keep_open", "close"] | None = None
    schedule: dict[str, Any] | None = None

    @field_validator("account_ids")
    @classmethod
    def unique_accounts(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    program_name: str | None = Field(default=None, min_length=1, max_length=160)
    account_ids: list[str] | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    browser_end_policy: Literal["keep_open", "close"] | None = None
    schedule: dict[str, Any] | None = None

    @field_validator("account_ids")
    @classmethod
    def unique_accounts(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class AISettingsUpdate(BaseModel):
    provider: str = Field(default="openai", max_length=80)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    model: str = Field(default="", max_length=160)
    timeout_seconds: float = Field(default=30, ge=1, le=300)


class AITestRequest(BaseModel):
    prompt: str = Field(default="请回复：连接测试成功", min_length=1, max_length=2000)


class AITemplateUpsert(BaseModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=160)
    system_prompt: str = Field(default="", max_length=20000)
    user_prompt: str = Field(min_length=1, max_length=20000)
    variables: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=160)
    enabled: bool = True


class RuntimeSettingsUpdate(BaseModel):
    max_concurrent_browser_tasks: int = Field(ge=1, le=100)
    cancellation_poll_interval_seconds: float = Field(ge=0.1, le=60)
    default_task_timeout_seconds: int = Field(ge=30, le=86400)
    browser_acquire_timeout_seconds: int = Field(ge=5, le=1800)
    default_browser_end_policy: Literal["keep_open", "close"]
    task_log_retention_days: int = Field(ge=1, le=3650)
    queue_poll_interval_seconds: float = Field(ge=0.1, le=60)
