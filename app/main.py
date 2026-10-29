from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.services.retrieval.db import create_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One pooled client for outbound LLM calls; redirects stay off so the API key header
    # is never forwarded to another host.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        app.state.http_client = client
        pool = create_pool(get_settings())
        app.state.db_pool = pool
        if pool is None:
            yield
            return
        # Opened lazily (min_size=0): startup never blocks on the database.
        await pool.open(wait=False)
        try:
            yield
        finally:
            await pool.close()


app = FastAPI(title="Journie API", lifespan=lifespan)
register_error_handlers(app)
app.include_router(api_router)
