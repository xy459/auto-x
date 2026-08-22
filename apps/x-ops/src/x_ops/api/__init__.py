"""Management API router."""
from __future__ import annotations

from fastapi import APIRouter

from . import accounts, ai, dashboard, login_jobs, settings, task_programs, task_runs, tasks

router = APIRouter(prefix="/api")
router.include_router(dashboard.router)
router.include_router(accounts.router)
router.include_router(login_jobs.router)
router.include_router(task_programs.router)
router.include_router(tasks.router)
router.include_router(task_runs.router)
router.include_router(ai.router)
router.include_router(settings.router)

__all__ = ["router"]
