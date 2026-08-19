from __future__ import annotations

from typing import Any

RETRYABLE_CODES = {
    "ACTION_LOCKED",
    "ELEMENT_BLOCKED",
    "ELEMENT_NOT_VISIBLE",
    "IDEMPOTENCY_IN_PROGRESS",
    "PROFILE_LOAD_FAILED",
    "PROFILE_LOADING_TIMEOUT",
    "STATE_UNKNOWN",
    "TIMEOUT",
    "PAGE_NAVIGATED",
}
UNCERTAIN_CODES = {"SUBMISSION_RESULT_UNKNOWN"}


class CancellationSignalError(Exception):
    """Preserve a caller-specific exception raised by CancellationSignal.wait()."""

    def __init__(self, reason: BaseException) -> None:
        super().__init__(str(reason))
        self.reason = reason


class ActionError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        retryable: bool | None = None,
        uncertain: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.uncertain = code in UNCERTAIN_CODES if uncertain is None else uncertain

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "uncertain": self.uncertain,
            "details": self.details,
        }


def normalize_error(error: BaseException) -> ActionError:
    if isinstance(error, ActionError):
        return error
    name = error.__class__.__name__
    message = str(error)
    if name == "TimeoutError" or "timeout" in name.lower():
        return ActionError("TIMEOUT", message or "Playwright operation timed out.", retryable=True)
    lowered = message.lower()
    if name == "TargetClosedError" or any(
        marker in lowered
        for marker in (
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "context has been closed",
        )
    ):
        return ActionError("BROWSER_CLOSED_DURING_RUN", message or "The browser or page was closed during the action.")
    if "execution context was destroyed" in lowered or "because of a navigation" in lowered:
        return ActionError("PAGE_NAVIGATED", message, retryable=True)
    return ActionError("UNEXPECTED_ERROR", message or name, {"type": name})
