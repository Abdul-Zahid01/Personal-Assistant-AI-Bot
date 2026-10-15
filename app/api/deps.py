from __future__ import annotations

import hmac
from typing import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request

from app.assistant.tools import ToolContext
from app.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    settings = request.app.state.container.settings
    if not settings.API_KEY:
        if settings.is_dev:
            return  # open in local development only
        raise HTTPException(status_code=503, detail="Server is missing API_KEY.")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


async def get_ctx(container: Container = Depends(get_container)) -> AsyncIterator[ToolContext]:
    async with container.session_factory() as session:
        yield await container.make_context(session)
