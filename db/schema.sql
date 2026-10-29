-- POI retrieval store (spec section 2, 7). Idempotent: safe to run on every import.
-- Derived text columns are computed by scripts/import_parquet.py with app.services.retrieval.text,
-- so Postgres `unaccent` is not needed:
--   name_keys / name_doc / search_doc   norm(): accents folded (fallback, trigram fuzzy names)
--   name_keys_vi / name_text            fold_case(): accents kept
--   name_lex / search_lex               lexeme_vector(): accent-aware full-text (see text.py)
-- On Azure Flexible Server, pg_trgm must be in the `azure.extensions` allow-list first.

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE TABLE IF NOT EXISTS pois (
    id                    text PRIMARY KEY,
    name                  text NOT NULL,
    name_en               text,
    kind                  text NOT NULL,  -- display only
    category              text,           -- taxonomy top.sub; sections filter on it
    category_top          text GENERATED ALWAYS AS (split_part(category, '.', 1)) STORED,
    cuisine               text[] NOT NULL DEFAULT '{}',
    features              text[] NOT NULL DEFAULT '{}',
    michelin              text,
    latitude              double precision,
    longitude             double precision,
    ward                  text,
    address               text,
    city                  text,
    outside_region        boolean NOT NULL DEFAULT false,
    opening_hours_raw     text,
    opening_hours         jsonb,
    hours_confidence      text,
    hours_contradicted    boolean,
    price_raw             text,
    description           text,
    tier                  text,
    recommended_by_count  integer,
    confidence            double precision,
    recommendable         boolean NOT NULL DEFAULT true,
    website               text,
    phone                 text,
    name_keys             text[] NOT NULL DEFAULT '{}',
    name_doc              text NOT NULL DEFAULT '',
    search_doc            text NOT NULL DEFAULT '',
    name_keys_vi          text[] NOT NULL DEFAULT '{}',
    name_text             text NOT NULL DEFAULT '',
    name_lex              tsvector NOT NULL DEFAULT '',
    search_lex            tsvector NOT NULL DEFAULT '',
    data_version          text,
    imported_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pois_name_keys_idx ON pois USING gin (name_keys);
CREATE INDEX IF NOT EXISTS pois_name_doc_trgm_idx ON pois USING gin (name_doc gin_trgm_ops);
CREATE INDEX IF NOT EXISTS pois_search_doc_trgm_idx ON pois USING gin (search_doc gin_trgm_ops);

-- Upgrades for tables created by an earlier schema.sql.
ALTER TABLE pois ADD COLUMN IF NOT EXISTS name_keys_vi text[] NOT NULL DEFAULT '{}';
ALTER TABLE pois ADD COLUMN IF NOT EXISTS name_text text NOT NULL DEFAULT '';
ALTER TABLE pois ADD COLUMN IF NOT EXISTS name_lex tsvector NOT NULL DEFAULT '';
ALTER TABLE pois ADD COLUMN IF NOT EXISTS search_lex tsvector NOT NULL DEFAULT '';
-- Folded full-text search conflated phở/phố; replaced by search_lex (recomputed on import).
ALTER TABLE pois DROP COLUMN IF EXISTS search_tsv;
-- Sections moved from kind (+ is_bar) to the taxonomy category; bars are a category now.
ALTER TABLE pois ADD COLUMN IF NOT EXISTS category_top text
    GENERATED ALWAYS AS (split_part(category, '.', 1)) STORED;
ALTER TABLE pois DROP COLUMN IF EXISTS is_bar;
DROP INDEX IF EXISTS pois_kind_idx;

CREATE INDEX IF NOT EXISTS pois_name_keys_vi_idx ON pois USING gin (name_keys_vi);
CREATE INDEX IF NOT EXISTS pois_name_text_trgm_idx ON pois USING gin (name_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS pois_name_lex_idx ON pois USING gin (name_lex);
CREATE INDEX IF NOT EXISTS pois_search_lex_idx ON pois USING gin (search_lex);
-- A section node is either a whole category (food.cafe) or a top level (food).
CREATE INDEX IF NOT EXISTS pois_category_idx ON pois (category);
CREATE INDEX IF NOT EXISTS pois_category_top_idx ON pois (category_top);
