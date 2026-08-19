from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol


class AIServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AIService(Protocol):
    async def generate(self, *, template: str, variables: Mapping[str, Any]) -> str: ...


class DisabledAIService:
    async def generate(self, *, template: str, variables: Mapping[str, Any]) -> str:
        raise AIServiceError("AI_NOT_CONFIGURED", "AI service is not configured")


class StaticAIService:
    """Small deterministic implementation useful for embedding and tests."""

    def __init__(self, value: str | Callable[[str, Mapping[str, Any]], str]) -> None:
        self._value = value

    async def generate(self, *, template: str, variables: Mapping[str, Any]) -> str:
        try:
            result = self._value(template, variables) if callable(self._value) else self._value
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError("AI_PROVIDER_ERROR", str(exc), retryable=True) from exc
        if not isinstance(result, str) or not result.strip():
            raise AIServiceError("AI_EMPTY_RESPONSE", "AI provider returned empty text")
        return result.strip()
