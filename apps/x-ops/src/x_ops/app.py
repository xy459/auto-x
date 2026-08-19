"""FastAPI application assembly for the x-ops management console."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from browser_custom.app import app as browser_management_app
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .api.contracts import AdminAPIError, AdminServices, JsonObject
from .scheduler import TaskScheduler

LOGGER = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).with_name("web")


def create_app(
    services: AdminServices | None = None,
    *,
    enable_scheduler: bool | None = None,
) -> FastAPI:
    explicit_services = services is not None
    if enable_scheduler is None:
        enable_scheduler = not explicit_services

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if application.state.services is None:
            from .api.backend import build_default_services

            application.state.services = build_default_services()
        active_services = cast(AdminServices, application.state.services)
        scheduler = TaskScheduler(
            active_services.backend,
            poll_interval_seconds=float(
                active_services.runtime_settings.get().get(
                    "queue_poll_interval_seconds", 1.0
                )
            ),
            poll_interval_provider=lambda: float(
                active_services.runtime_settings.get().get(
                    "queue_poll_interval_seconds", 1.0
                )
            ),
        )
        application.state.scheduler = scheduler
        start = getattr(application.state.services.backend, "start", None)
        if start is not None:
            await start()
        scheduler_task = (
            asyncio.create_task(scheduler.run(), name="x-ops-scheduler")
            if enable_scheduler
            else None
        )
        try:
            yield
        finally:
            scheduler.stop()
            if scheduler_task is not None:
                await scheduler_task
            close = getattr(application.state.services.backend, "close", None)
            if close is not None:
                await close()

    application = FastAPI(
        title="x-ops",
        version="0.1.0",
        description="X/Twitter 多账户脚本式任务管理后台",
        lifespan=lifespan,
    )
    application.state.services = services
    application.state.scheduler = None
    application.include_router(router)
    application.mount("/assets", StaticFiles(directory=WEB_ROOT), name="assets")
    application.mount("/browser-custom", browser_management_app, name="browser-custom")

    @application.exception_handler(AdminAPIError)
    async def handle_admin_error(_request: Request, exc: AdminAPIError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @application.get("/api/health", tags=["系统"])
    async def health() -> JsonObject:
        return {"ok": True, "service": "x-ops"}

    @application.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    return application


app = create_app()
