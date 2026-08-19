from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .contracts import AdminServices, JsonObject
from .dependencies import get_services

router = APIRouter(tags=["任务程序"])


@router.get("/task-programs")
async def list_programs(services: AdminServices = Depends(get_services)) -> JsonObject:
    return {"programs": await services.backend.list_programs()}


@router.get("/task-programs/{program_name}")
async def get_program(
    program_name: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    program = await services.backend.get_program(program_name)
    if program is None:
        raise HTTPException(404, "任务程序不存在")
    return {"program": program}
