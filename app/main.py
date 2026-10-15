from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import integrations, routes
from app.channels.telegram import TelegramPoller
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.jobs.scheduler import Scheduler

log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


async def supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Keep a background loop alive: if it ever crashes, log it and start it again."""
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background task '%s' crashed; restarting in 5s", name)
            await asyncio.sleep(5)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_runtime()
    container = container or build_container(settings)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.start()
        jobs: list[asyncio.Task] = []
        if settings.SCHEDULER_ENABLED:
            jobs.append(asyncio.create_task(supervise("scheduler", Scheduler(container).run), name="scheduler"))
        if container.telegram is not None and settings.TELEGRAM_MODE.lower() == "polling":
            jobs.append(asyncio.create_task(supervise("telegram", TelegramPoller(container).run), name="telegram"))
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await container.close()

    app = FastAPI(title="Personal AI Assistant", version="1.0.0", lifespan=lifespan)
    app.state.container = container
    app.include_router(routes.public)
    app.include_router(routes.router)
    app.include_router(integrations.router)
    app.include_router(integrations.callbacks)

    no_cache = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=no_cache)

    # The service worker must be served from the root so it can control the whole app.
    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers=no_cache)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers=no_cache)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    return app


def app_factory() -> FastAPI:
    """uvicorn entry point:  uvicorn app.main:app_factory --factory"""
    return create_app()
