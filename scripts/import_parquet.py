"""Import a POI Parquet file into Postgres (upsert by id; safe to re-run).

    uv run --group data python scripts/import_parquet.py data/places_1000.parquet \
        --schema retrieval_test [--data-version 2026-10-28] [--prune] [--dry-run] \
        [--taxonomy data/taxonomy.v1.yaml] [--no-strict-taxonomy] [--no-embed]

Reads DATABASE_URL from the environment / .env. Creates the schema, the pg_trgm extension and
the ``pois`` table (db/schema.sql) if missing. ``--prune`` deletes rows whose id is not in the
file, so the table mirrors one dataset release.

Every row's ``taxonomy_version`` and ``category`` (after overrides) are checked against the
taxonomy file. Problems are fatal; ``--no-strict-taxonomy`` prints them as warnings instead
(for a dataset with a known issue, declared under interest_map ``taxonomy_gaps``).

Then every row whose name/description changed (``embedding_hash``) is embedded with Gemini for
semantic search: GEMINI_API_KEY2 first, GEMINI_API_KEY on HTTP 429 (the API uses them the other
way round, so the two free-tier quotas are shared). ``--no-embed`` skips it.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx
import pyarrow.parquet as pq
from psycopg import Connection, connect, sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings
from app.services.gemini import EMBED_BATCH_LIMIT, GeminiRateLimitError, embed_all
from app.services.retrieval.config import load_overrides
from app.services.retrieval.db import SCHEMA_NAME
from app.services.retrieval.ingest import prepare_row, taxonomy_issues
from app.services.retrieval.semantic import document_hash, document_text, vector_literal
from app.services.retrieval.taxonomy import load_taxonomy

SCHEMA_SQL = ROOT / "db" / "schema.sql"
DEFAULT_TAXONOMY = ROOT / "data" / "taxonomy.v1.yaml"
# Built as tsvector literals in Python (text.lexeme_vector); cast explicitly on insert.
TSVECTOR_COLUMNS = frozenset({"name_lex", "search_lex"})
# Free tier: 100 embedding requests per minute per key, and each text of a batch counts as one.
RATE_LIMIT_WAIT_S = 61
RATE_LIMIT_ATTEMPTS = 5  # per batch; a daily quota stop ends the run (done batches are kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("parquet", type=Path)
    parser.add_argument("--schema", default=None, help="Target schema (default DATABASE_SCHEMA)")
    parser.add_argument("--data-version", default=None, help="Defaults to the file name stem")
    parser.add_argument("--prune", action="store_true", help="Delete ids missing from the file")
    parser.add_argument("--dry-run", action="store_true", help="Transform only; print a summary")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument(
        "--strict-taxonomy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail on rows that break the taxonomy (default); --no-strict-taxonomy only warns",
    )
    parser.add_argument("--no-embed", action="store_true", help="Skip Gemini embeddings")
    args = parser.parse_args()

    settings = Settings()
    schema = args.schema or settings.database_schema
    if not SCHEMA_NAME.fullmatch(schema):
        parser.error(f"invalid schema name {schema!r}")
    overrides = load_overrides()
    version = args.data_version or args.parquet.stem
    raw_rows = pq.read_table(args.parquet).to_pylist()
    rows = [prepare_row(row, overrides, version) for row in raw_rows]
    unused = set(overrides) - {row["id"] for row in rows}
    print(f"{len(rows)} rows from {args.parquet}")
    if unused:
        print(f"warning: overrides for ids not in this file: {sorted(unused)}")
    dropped_hours = [
        row["id"]
        for raw, row in zip(raw_rows, rows)
        if raw.get("opening_hours_normalized") and row["opening_hours"] is None
    ]
    if dropped_hours:
        print(f"warning: unreadable opening hours stored as unknown: {dropped_hours}")
    issues = taxonomy_issues(
        ((raw.get("taxonomy_version"), row["category"]) for raw, row in zip(raw_rows, rows)),
        load_taxonomy(args.taxonomy),
    )
    for problem, count in issues.most_common():
        print(f"{'error' if args.strict_taxonomy else 'warning'}: {count} rows: {problem}")
    if issues and args.strict_taxonomy:
        return 1
    if args.dry_run:
        return 0
    if settings.database_url is None:
        parser.error("DATABASE_URL is not set")

    columns = list(rows[0])
    upsert = sql.SQL(
        "INSERT INTO pois ({cols}) VALUES ({vals}) "
        "ON CONFLICT (id) DO UPDATE SET {updates}, imported_at = now()"
    ).format(
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        vals=sql.SQL(", ").join(
            sql.SQL("{}::tsvector").format(sql.Placeholder(c))
            if c in TSVECTOR_COLUMNS
            else sql.Placeholder(c)
            for c in columns
        ),
        updates=sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in columns if c != "id"
        ),
    )
    with connect(settings.database_url.get_secret_value()) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        with conn.cursor() as cur:
            cur.executemany(
                upsert,
                [{**row, "opening_hours": Jsonb(row["opening_hours"])} for row in rows],
            )
            if args.prune:
                cur.execute(
                    "DELETE FROM pois WHERE NOT (id = ANY(%s))", ([row["id"] for row in rows],)
                )
                print(f"pruned {cur.rowcount} rows")
            cur.execute("SELECT count(*) FROM pois")
            count = cur.fetchone()
        conn.commit()
        print(f"{schema}.pois now has {count[0] if count else 0} rows")
        if not args.no_embed:
            embed_changed(conn, settings)
    return 0


def embed_changed(conn: Connection, settings: Settings) -> None:
    """Embed rows whose document text changed since their last embedding; commit per batch so
    a quota stop keeps the work done."""
    keys = [k.get_secret_value() for k in (settings.gemini_api_key2, settings.gemini_api_key) if k]
    if not keys:
        print("warning: no GEMINI_API_KEY2 / GEMINI_API_KEY: semantic search has no embeddings")
        return
    model = settings.gemini_embedding_model
    rows = conn.execute("SELECT id, name, description, embedding_hash FROM pois").fetchall()
    todo = []
    for poi_id, name, description, stored in rows:
        text = document_text(name, description)
        if document_hash(model, text) != stored:
            todo.append((poi_id, text))
    print(f"{len(todo)} of {len(rows)} rows to embed with {model}")
    for start in range(0, len(todo), EMBED_BATCH_LIMIT):  # one API call per batch
        chunk = todo[start : start + EMBED_BATCH_LIMIT]
        vectors = _embed_waiting([text for _, text in chunk], settings, keys)
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE pois SET embedding = %s::vector, embedding_hash = %s WHERE id = %s",
                [
                    (vector_literal(vector), document_hash(model, text), poi_id)
                    for (poi_id, text), vector in zip(chunk, vectors, strict=True)
                ],
            )
        conn.commit()
        print(f"embedded {start + len(chunk)}/{len(todo)}")


def _embed_waiting(texts: list[str], settings: Settings, keys: list[str]) -> list[list[float]]:
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        try:
            return asyncio.run(_embed(texts, settings, keys))
        except GeminiRateLimitError:
            if attempt == RATE_LIMIT_ATTEMPTS:
                raise
            print(f"rate limited on every key; waiting {RATE_LIMIT_WAIT_S}s")
            time.sleep(RATE_LIMIT_WAIT_S)
    raise AssertionError("unreachable")


async def _embed(texts: list[str], settings: Settings, keys: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient() as http:
        return await embed_all(http, settings, texts, keys)


if __name__ == "__main__":
    raise SystemExit(main())
