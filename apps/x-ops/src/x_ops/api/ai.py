from __future__ import annotations

from fastapi import APIRouter, Depends, status

from .contracts import AdminServices, JsonObject
from .dependencies import get_services
from .schemas import AISettingsUpdate, AITemplateUpsert, AITestRequest

router = APIRouter(tags=["AI 服务"])


@router.get("/ai/settings")
def get_ai_settings(services: AdminServices = Depends(get_services)) -> JsonObject:
    return services.ai_settings.public()


@router.put("/ai/settings")
def update_ai_settings(
    body: AISettingsUpdate, services: AdminServices = Depends(get_services)
) -> JsonObject:
    return services.ai_settings.update(body.model_dump(exclude_unset=True))


@router.post("/ai/test")
async def test_ai(
    body: AITestRequest, services: AdminServices = Depends(get_services)
) -> JsonObject:
    return await services.test_ai(body.model_dump())


@router.get("/ai/templates")
def list_templates(services: AdminServices = Depends(get_services)) -> JsonObject:
    return {"templates": services.ai_settings.templates()}


@router.post("/ai/templates", status_code=status.HTTP_201_CREATED)
def save_template(
    body: AITemplateUpsert, services: AdminServices = Depends(get_services)
) -> JsonObject:
    return {"template": services.ai_settings.save_template(body.model_dump())}
