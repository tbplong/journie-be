# Journie Backend

A FastAPI backend using Python 3.13+ and [uv](https://docs.astral.sh/uv/).

## Getting started

Run these commands from the project root:

```powershell
uv sync
uv run fastapi dev app/main.py
```

The development server runs at http://127.0.0.1:8000 with automatic reload.

- Interactive API documentation: http://127.0.0.1:8000/docs
- Alternative API documentation: http://127.0.0.1:8000/redoc
- Root endpoint: `GET /` returns `{"Hello": "World"}`
- Health check: `GET /health` returns `{"status": "ok"}`

To run without development reload:

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Project structure

```text
app/
├── main.py              # Creates the FastAPI app and registers routers
├── api/
│   ├── router.py        # Collects feature routers
│   └── routes/          # HTTP endpoints grouped by feature
├── core/                # Shared configuration and security utilities
├── schemas/             # Pydantic request and response models
└── services/            # Business logic
tests/                   # Application tests
.env.example             # Template for future environment settings
pyproject.toml           # Project metadata and dependencies
uv.lock                  # Locked dependency versions
```

Each Python package includes an `__init__.py` file.

## Adding a feature

1. Define request and response models in `app/schemas/<feature>.py`.
2. Implement business logic in `app/services/<feature>.py`.
3. Create an `APIRouter` in `app/api/routes/<feature>.py`, keeping HTTP-specific
   handling in the route and calling services for business logic.
4. Import and register the router in `app/api/router.py` using
   `api_router.include_router(...)`.
5. Add relevant tests under `tests/` as functionality grows.

Add database sessions and ORM models when choosing a database integration.

## Configuration

No environment variables are currently required. When adding settings, place
configuration code in `app/core/` and document the variables in `.env.example`.
A local `.env` file is ignored by Git. Environment-file loading must be configured
when settings are introduced.
