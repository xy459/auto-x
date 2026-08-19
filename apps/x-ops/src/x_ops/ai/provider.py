"""OpenAI-compatible AIService backed by console settings and templates."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from x_ops.task_sdk.ai import AIServiceError

from .settings import AIConfigStore


class ConfiguredAIService:
    def __init__(self, settings: AIConfigStore) -> None:
        self.settings = settings

    async def generate(self, *, template: str, variables: Mapping[str, Any]) -> str:
        config = self.settings.get()
        selected = next(
            (
                item
                for item in self.settings.templates()
                if item.get("id") == template and item.get("enabled", True)
            ),
            None,
        )
        if selected is None:
            raise AIServiceError("AI_TEMPLATE_NOT_FOUND", f"AI template not found: {template}")
        try:
            system_prompt = _render(str(selected.get("system_prompt") or ""), variables)
            user_prompt = _render(str(selected.get("user_prompt") or ""), variables)
        except KeyError as exc:
            raise AIServiceError("AI_TEMPLATE_VARIABLE_MISSING", str(exc)) from exc
        base_url = str(config.get("base_url") or "").rstrip("/")
        api_key = self.settings.secret_store.get()
        model = str(selected.get("model") or config.get("model") or "")
        if not base_url or not api_key or not model:
            raise AIServiceError("AI_NOT_CONFIGURED", "AI provider is not fully configured")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        try:
            async with httpx.AsyncClient(timeout=float(config.get("timeout_seconds") or 30)) as client:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "messages": messages},
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException as exc:
            raise AIServiceError("AI_TIMEOUT", "AI provider timed out", retryable=True) from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise AIServiceError("AI_PROVIDER_ERROR", str(exc), retryable=True) from exc
        if not isinstance(content, str) or not content.strip():
            raise AIServiceError("AI_EMPTY_RESPONSE", "AI provider returned empty text")
        return content.strip()


def _render(template: str, variables: Mapping[str, Any]) -> str:
    import re

    names = _variable_names(template)
    for name in names:
        if name not in variables:
            raise KeyError(f"missing template variable: {name}")
    return re.sub(
        r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
        lambda match: str(variables[match.group(1)]),
        template,
    )


def _variable_names(template: str) -> set[str]:
    import re

    return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", template))
