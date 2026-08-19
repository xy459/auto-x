from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from .contracts import AdminServices, JsonObject
from .dependencies import get_services
from .schemas import TaskCreate, TaskUpdate

router = APIRouter(tags=["任务管理"])


@router.get("/tasks")
async def list_tasks(
    search: str | None = None,
    program_name: str | None = None,
    enabled: bool | None = None,
    account_id: str | None = None,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    filters = {
        key: value
        for key, value in {
            "search": search,
            "program_name": program_name,
            "enabled": enabled,
            "account_id": account_id,
        }.items()
        if value is not None
    }
    return {"tasks": await services.backend.list_tasks(filters)}


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate, services: AdminServices = Depends(get_services)
) -> JsonObject:
    task = await services.backend.create_task(body.model_dump())
    return {"task": task}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    task = await services.backend.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {"task": task}


@router.put("/tasks/{task_id}")
async def update_task(
    task_id: str,
    body: TaskUpdate,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    task = await services.backend.update_task(task_id, body.model_dump(exclude_unset=True))
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {"task": task}


@router.post("/tasks/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_task(
    task_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    result = await services.backend.trigger_task(task_id, "manual")
    if result is None:
        raise HTTPException(404, "任务不存在")
    return result


@router.post("/tasks/{task_id}/clone", status_code=status.HTTP_201_CREATED)
async def clone_task(
    task_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    task = await services.backend.clone_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {"task": task}


async def _set_enabled(
    task_id: str, enabled: bool, services: AdminServices
) -> JsonObject:
    task = await services.backend.set_task_enabled(task_id, enabled)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {"task": task}


@router.post("/tasks/{task_id}/enable")
async def enable_task(
    task_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    return await _set_enabled(task_id, True, services)


@router.post("/tasks/{task_id}/disable")
async def disable_task(
    task_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    return await _set_enabled(task_id, False, services)
