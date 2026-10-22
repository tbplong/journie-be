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
- Chat (travel-constraint extraction): `POST /chat` (see below; needs `GEMINI_API_KEY`)

## Travel-constraint extraction

`POST /chat` turns a free-text travel request into structured constraints. It is
the production form of the journie-data LLM pipeline (`src/journie-llm`), using the
best-measured combination, run6 + repair-v3 (83.6% exact cases on dataset v3):

1. System prompt `semantic-v3`, copied verbatim; only the reference date is filled in per
   request (default: today in Asia/Ho_Chi_Minh).
2. Gemini (`gemini-3.5-flash-lite`, Google AI Studio) with native `responseJsonSchema`,
   temperature 0.
3. Strict Pydantic validation of the `Extraction` contract.
4. Deterministic `repair-v3` processing (alias canonicalization, list/atom sync, scalar
   from constraint, observed conflicts, `missing_fields`) and `consistency-v2` diagnostics.

```powershell
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" `
  -d '{"text": "Đi Đà Lạt 3 ngày từ 2026-11-20, tối đa 3tr, thích cà phê"}'
```

The response has `extraction` (the contract), `repairs` (steps applied), `issues`
(unresolved diagnostics), and `meta` (model, prompt/processing versions, reference date,
latency, token usage). Instruction attacks inside `text` return an empty extraction by design.

Errors use `application/problem+json`: `502 invalid-llm-output` / `llm-upstream-error`,
`503 llm-not-configured` / `llm-rate-limited` (with `Retry-After`), `504 llm-timeout`.
There are no automatic retries, matching the benchmark.

When changing the prompt, `Extraction` schema, or processing in `app/services/extraction/`,
re-benchmark in journie-data first and bump the version constant.

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

Settings live in `app/core/config.py` and are read from the environment or a local `.env`
(see `.env.example`); a local `.env` file is ignored by Git. Document new variables in
`.env.example`.

## Tests

```powershell
uv run pytest
uv run ruff check app tests
```

Tests mock the Gemini API; no key or network access is needed.
