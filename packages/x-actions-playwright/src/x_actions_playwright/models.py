from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

ActionStatus = Literal["success", "skipped", "navigating", "uncertain", "cancelled", "failed"]
Access = Literal["read", "write"]
RetryPolicy = Literal["safe", "never"]


@dataclass(frozen=True, slots=True)
class FailureMode:
    code: str
    description: str
    retryable: bool = False
    uncertain: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    id: str
    category: str
    method: str
    label: str
    handler: str
    access: Access
    retry_policy: RetryPolicy
    target_type: str
    idempotent: bool
    enabled: bool
    confirmation: Literal["none", "required"]
    requires_tweet: bool = False
    requires_comment: bool = False
    deprecated: bool = False
    replaced_by: str | None = None
    disabled_reason: str | None = None
    failure_modes: tuple[FailureMode, ...] = ()
    edge_cases: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["retryPolicy"] = result.pop("retry_policy")
        result["targetType"] = result.pop("target_type")
        result["requiresTweet"] = result.pop("requires_tweet")
        result["requiresComment"] = result.pop("requires_comment")
        result["replacedBy"] = result.pop("replaced_by")
        result["disabledReason"] = result.pop("disabled_reason")
        result["failureModes"] = result.pop("failure_modes")
        result["edgeCases"] = result.pop("edge_cases")
        result["inputSchema"] = result.pop("input_schema")
        result["outputSchema"] = result.pop("output_schema")
        return result


@dataclass(slots=True)
class ExecutionOptions:
    dry_run: bool = False
    confirm_live: bool = False
    idempotency_key: str | None = None
    timeout_ms: int = 10_000
    cancellation: asyncio.Event | None = None
    account_scope: str | None = None
    capture_failure: bool = False
    artifact_hook: Any | None = None


@dataclass(slots=True)
class ActionResult:
    status: ActionStatus
    action: str
    category: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkflowStep:
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    delay_after_ms: int = 0
    when: Any | None = None
    continue_on_uncertain: bool = False
    continue_on_navigating: bool = False
