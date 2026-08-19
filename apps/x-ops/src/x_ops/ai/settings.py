"""AI provider and prompt-template settings for the management console."""
from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from x_ops.api.config_store import JsonSettingsStore

from .secrets import AISecretStore

DEFAULT_AI_SETTINGS: dict[str, Any] = {
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "model": "",
    "timeout_seconds": 30.0,
    "templates": [
        {
            "id": "reply_to_post",
            "name": "回复帖子",
            "system_prompt": "你是一个自然、简洁的社交媒体助手。",
            "user_prompt": "请回复这条帖子：{{post_text}}",
            "variables": ["post_text"],
            "model": None,
            "enabled": True,
        }
    ],
}


class AIConfigStore(JsonSettingsStore):
    def __init__(self, path: Path, secret_store: AISecretStore | None = None):
        super().__init__(path, DEFAULT_AI_SETTINGS)
        self.secret_store = secret_store or AISecretStore(path.with_name("ai-api-key.secret"))
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            legacy_key = raw.pop("api_key", None) if isinstance(raw, dict) else None
            if legacy_key and not self.secret_store.get():
                self.secret_store.set(str(legacy_key))
            if legacy_key:
                super().update(raw)

    def get(self) -> dict[str, Any]:
        settings = super().get()
        settings.pop("api_key", None)
        return settings

    def public(self) -> dict[str, Any]:
        settings = self.get()
        settings.pop("api_key", None)  # scrub files written by pre-release builds
        key = self.secret_store.get()
        settings.pop("templates", None)
        settings["api_key_configured"] = bool(key)
        return settings

    def update(self, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        api_key = payload.pop("api_key", None)
        if api_key:
            self.secret_store.set(str(api_key))
        # Never preserve a key that may have been written by an old build.
        result = super().update(payload)
        result.pop("api_key", None)
        result.pop("templates", None)
        result["api_key_configured"] = bool(
            self.secret_store.get()
        )
        return result

    def templates(self) -> list[dict[str, Any]]:
        templates = self.get().get("templates", [])
        return deepcopy(templates if isinstance(templates, list) else [])

    def save_template(self, values: Mapping[str, Any]) -> dict[str, Any]:
        template = dict(values)
        settings = self.get()
        templates = settings.get("templates", [])
        if not isinstance(templates, list):
            templates = []
        replaced = False
        next_templates = []
        for item in templates:
            if isinstance(item, dict) and item.get("id") == template["id"]:
                next_templates.append(template)
                replaced = True
            else:
                next_templates.append(item)
        if not replaced:
            next_templates.append(template)
        super().update({"templates": next_templates})
        return deepcopy(template)


async def test_ai_connection(
    store: AIConfigStore, request: Mapping[str, Any]
) -> dict[str, Any]:
    settings = store.get()
    provider = str(settings.get("provider") or "openai")
    base_url = str(settings.get("base_url") or "").rstrip("/")
    model = str(settings.get("model") or "")
    api_key = store.secret_store.get()
    if not base_url or not model or not api_key:
        return {
            "ok": False,
            "provider": provider,
            "message": "请先配置 API 地址、API Key 和默认模型",
        }
    prompt = str(request.get("prompt") or "请回复：连接测试成功")
    timeout = float(settings.get("timeout_seconds") or 30)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "provider": provider, "model": model, "message": content}
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "provider": provider,
            "message": f"AI 服务返回 HTTP {exc.response.status_code}",
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return {"ok": False, "provider": provider, "message": f"连接失败：{exc}"}
