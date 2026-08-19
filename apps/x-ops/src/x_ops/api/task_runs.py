from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .contracts import AdminServices, JsonObject
from .dependencies import get_services

router = APIRouter(tags=["运行记录"])


@router.get("/task-runs")
async def list_runs(
    task_id: str | None = None,
    program_name: str | None = None,
    account_id: str | None = None,
    run_status: str | None = Query(default=None, alias="status"),
    trigger: str | None = None,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    filters = {
        key: value
        for key, value in {
            "task_id": task_id,
            "program_name": program_name,
            "account_id": account_id,
            "status": run_status,
            "trigger": trigger,
        }.items()
        if value is not None
    }
    return {"runs": await services.backend.list_runs(filters)}


@router.get("/task-runs/{run_id}")
async def get_run(
    run_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    run = await services.backend.get_run(run_id)
    if run is None:
        raise HTTPException(404, "运行记录不存在")
    return {"run": run}


@router.get("/task-runs/{run_id}/logs")
async def list_logs(
    run_id: str,
    after: str | None = None,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    if await services.backend.get_run(run_id) is None:
        raise HTTPException(404, "运行记录不存在")
    return {"logs": await services.backend.list_logs(run_id, after)}


@router.post("/task-runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    result = await services.backend.cancel_run(run_id)
    if result is None:
        raise HTTPException(404, "运行记录不存在")
    return result


@router.post("/task-runs/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
async def rerun(
    run_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    result = await services.backend.rerun(run_id)
    if result is None:
        raise HTTPException(404, "运行记录不存在")
    return result
