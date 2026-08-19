from __future__ import annotations

from fastapi import APIRouter, Depends

from .contracts import AdminServices, JsonObject
from .dependencies import get_services
from .schemas import RuntimeSettingsUpdate

router = APIRouter(tags=["系统设置"])


@router.get("/settings/runtime")
async def get_runtime_settings(
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    return {
        "settings": services.runtime_settings.get(),
        "status": await services.backend.runtime_status(),
    }


@router.put("/settings/runtime")
async def update_runtime_settings(
    body: RuntimeSettingsUpdate, services: AdminServices = Depends(get_services)
) -> JsonObject:
    settings = services.runtime_settings.update(body.model_dump())
    apply = getattr(services.backend, "apply_runtime_settings", None)
    if apply is not None:
        await apply(settings)
    return {"settings": settings}
