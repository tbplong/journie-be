"""RFC 9457 (formerly 7807) problem responses for application errors."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PROBLEM_CONTENT_TYPE = "application/problem+json"


class ProblemError(Exception):
    """An error rendered as ``application/problem+json``.

    ``slug`` becomes the stable problem ``type`` URI (``/problems/<slug>``).
    """

    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail
        self.headers = headers


async def problem_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return JSONResponse(
        status_code=exc.status,
        content={
            "type": f"/problems/{exc.slug}",
            "title": exc.title,
            "status": exc.status,
            "detail": exc.detail,
            "instance": request.url.path,
        },
        headers=exc.headers,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def problem_doc(description: str, instance: str) -> dict[str, Any]:
    """OpenAPI ``responses`` entry documenting a problem response of a route."""
    return {
        "description": description,
        "content": {
            PROBLEM_CONTENT_TYPE: {
                "example": {
                    "type": "/problems/<slug>",
                    "title": "...",
                    "status": 0,
                    "detail": "...",
                    "instance": instance,
                }
            }
        },
    }


async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422, without echoing ``input`` when it holds NaN or Infinity: Python's JSON
    parser accepts them, the strict JSON renderer does not (that was a 500)."""
    assert isinstance(exc, RequestValidationError)
    try:
        return await request_validation_exception_handler(request, exc)
    except ValueError:
        errors = [
            {k: v for k, v in error.items() if k not in ("input", "ctx")} for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, problem_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
