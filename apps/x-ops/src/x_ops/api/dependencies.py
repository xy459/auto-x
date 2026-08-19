"""FastAPI dependency accessors."""
from __future__ import annotations

from typing import cast

from fastapi import Request

from .contracts import AdminServices


def get_services(request: Request) -> AdminServices:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise RuntimeError("x-ops 管理后台服务尚未初始化")
    return cast(AdminServices, services)
