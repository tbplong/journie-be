from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.router import api_router
from app.core.errors import register_error_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One pooled client for outbound LLM calls; redirects stay off so the API key header
    # is never forwarded to another host.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        app.state.http_client = client
        yield


app = FastAPI(title="Journie API", lifespan=lifespan)
register_error_handlers(app)
app.include_router(api_router)
