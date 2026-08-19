from __future__ import annotations

from fastapi import APIRouter, Depends

from .contracts import AdminServices, JsonObject
from .dependencies import get_services

router = APIRouter(tags=["概览"])


@router.get("/dashboard")
async def dashboard(services: AdminServices = Depends(get_services)) -> JsonObject:
    return await services.backend.dashboard()
