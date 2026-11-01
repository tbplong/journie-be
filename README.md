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
- POI retrieval: `POST /retrieval` (see below; needs `DATABASE_URL`)

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

## POI retrieval

`POST /retrieval` takes the `extraction` from `/chat` and returns Ho Chi Minh City places:
`pinned` (resolved `must_visit`), `disambiguation` (names with several close matches),
`not_found`, ranked `sections` (one per interest, in the traveller's order), and `warnings`.
There is no LLM: names are matched with `pg_trgm`; interests by taxonomy category; structured
columns (`config/keyword_map.v1.yaml`: Michelin, vegetarian, bar, cuisines) or full-text search
serve the other interests. Every `reason` is built from stored columns, and `score_parts` shows the
`match` / `quality` / `time_fit` / `proximity` split. Opening hours exclude a place only when
they are trusted (medium/high confidence, not contradicted); doubtful ones are flagged
`hours_maybe_closed`. Page one section with `section` + `offset` + `limit`. Pass
`selected_ids` to rank places near the ones already picked higher.

Sections come from the data's `category` (`top.sub`, taxonomy `data/taxonomy.v1.yaml`), not
from `kind`, which is display only. `config/interest_map.v2.yaml` maps each standard interest to
taxonomy nodes: a whole top level (`food`, so a new `food.*` sub joins with no code change) or one
sub (`food.cafe`). When two requested interests cover a place, the deeper node wins (Cà phê over
Ẩm thực). A non-standard interest such as "phở" finds its *home*, the node holding at least 60% of
its hits weighted by evidence (name or structured hit 1, description-only 0.3). Description-only
hits outside the home are dropped, so a market that mentions phở is not a phở place. The keyword
merges into a requested section covering its home, with its hits listed first; otherwise it gets
its own section.

Text matching follows one accent rule per word (`app/services/retrieval/text.py`): a word typed
with diacritics matches the same word as written, or a word written without any ("phở" matches
"phở" and "pho", never "phố"). A word typed without diacritics matches every accented form. Each
stored word is indexed both as written and folded, at the same position, so the rule runs in
Postgres full-text search with no word lists.

Code lives in `app/services/retrieval/`. The pure steps (`hours`, `names.classify`, `interests`,
`scoring`, `sections`, `ingest`) are tested without a database.

Data setup (needs `DATABASE_URL`; on Azure, add `pg_trgm` to the `azure.extensions`
allow-list first):

```powershell
uv run --group data python scripts/coverage_report.py data/places_100.parquet   # check data issues
uv run --group data python scripts/import_parquet.py data/places_100.parquet --schema retrieval_test
uv run pytest -m integration                         # SQL paths against retrieval_test
uv run python eval/run_eval.py --schema retrieval_test   # name accuracy, P@5, nDCG@5, violations
```

The importer upserts by `id` (`--prune` drops ids missing from the file), applies
`config/poi_overrides.csv` (hand-maintained `category` fixes such as bars tagged `food.cafe`) and
checks every `category` against the taxonomy. Problems are warnings; pass `--strict-taxonomy` to
make them fatal. Known demo-data issue: 9 places use the pre-v1 value `culture.place_of_worship`,
declared under `taxonomy_gaps` in the interest map so Văn hoá still lists them. Regenerate the data
with the current journie-data pipeline, then drop the gap and the bar overrides. When changing a
config file in a way that changes results, bump its `version` and re-run the eval.
Deployment: `vercel.json` pins region `sin1`; keep Postgres in Southeast Asia. `POST /chat` has no
login and every call is a paid Gemini request: add a Vercel Firewall rate-limit rule on `/chat`
(dashboard, Firewall → custom rule, e.g. 20 requests/minute per IP) so one client cannot use up
the quota.

To run without development reload:

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On Windows, psycopg's async mode needs a selector event loop. `fastapi dev` already uses one
(reload), but plain uvicorn needs `--loop asyncio:SelectorEventLoop`.

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

Tests mock the Gemini API; no key or network access is needed. Tests marked `integration`
(a real Postgres) are deselected by default.
