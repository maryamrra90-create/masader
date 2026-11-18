-- Migration 0001: the architecture v1.2 logical data model (section E), unchanged from the schema that
-- experiments E1 and E2 ran against. Later migrations only add to it (expand, then contract).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS masader;
SET search_path = masader, public;

-- ---------- enums ----------
CREATE TYPE role_t          AS ENUM ('requester', 'reviewer', 'admin');
CREATE TYPE source_kind_t   AS ENUM ('gdelt', 'rss', 'worldbank');
CREATE TYPE tier_t          AS ENUM ('A', 'B', 'C');
CREATE TYPE request_status_t AS ENUM ('needs_clarification', 'queued', 'running', 'delivered',
                                      'in_review', 'failed', 'rejected', 'waiting_provider');
CREATE TYPE publication_t   AS ENUM ('private', 'in_review', 'published', 'withdrawn', 'superseded');
CREATE TYPE claim_type_t    AS ENUM ('fact', 'attributed', 'opinion', 'forecast', 'other');
CREATE TYPE verdict_t       AS ENUM ('supports', 'contradicts', 'not_addressed');
CREATE TYPE label_t         AS ENUM ('corroborated', 'single_source', 'disputed', 'insufficient');
CREATE TYPE review_status_t AS ENUM ('machine_checked', 'human_reviewed');
CREATE TYPE purpose_t       AS ENUM ('dev', 'eval', 'live', 'stage2', 'other');
CREATE TYPE stage_t         AS ENUM ('plan', 'collect', 'dedup', 'extract', 'verify', 'label',
                                     'write', 'validate', 'render');
CREATE TYPE stage_status_t  AS ENUM ('pending', 'running', 'done', 'failed', 'skipped');

-- ---------- access ----------
CREATE TABLE api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    holder_name   text        NOT NULL,
    holder_email  text        NOT NULL,
    role          role_t      NOT NULL,
    key_prefix    char(8)     NOT NULL UNIQUE,          -- first 8 chars of the key, for lookup
    key_hash      bytea       NOT NULL,                 -- HMAC-SHA256(pepper, key)
    rate_per_hour int         NOT NULL DEFAULT 10,
    created_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);

-- ---------- Tier 1: raw source material ----------
CREATE TABLE sources (                                  -- the source register, mirrored from config/sources.yaml
    id               smallserial PRIMARY KEY,
    code             text        NOT NULL UNIQUE,       -- e.g. 'rss.thenational.en'
    kind             source_kind_t NOT NULL,
    outlet_name      text        NOT NULL,
    outlet_domain    text,
    language         char(2)     NOT NULL,
    tier             tier_t      NOT NULL,
    tier_reason      text        NOT NULL,
    register_version text        NOT NULL,              -- git hash of config/sources.yaml
    allow_fulltext   boolean     NOT NULL DEFAULT false,
    fulltext_days    smallint    NOT NULL DEFAULT 30,
    active           boolean     NOT NULL DEFAULT true
);

CREATE TABLE origin_groups (
    id         bigserial PRIMARY KEY,
    method     text        NOT NULL CHECK (method IN ('single', 'embedding', 'minhash', 'attribution', 'quote_set')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE items (
    id              bigserial PRIMARY KEY,
    source_id       smallint    NOT NULL REFERENCES sources(id),
    url             text        NOT NULL,
    url_hash        bytea       NOT NULL,               -- sha256 of the normalised URL
    content_hash    bytea       NOT NULL,               -- sha256 of the cleaned text (exact duplicates)
    title           text        NOT NULL,
    excerpt         text        NOT NULL,               -- <= 600 chars, kept 12 months
    full_text       text,                               -- purged after sources.fulltext_days
    lang            char(2)     NOT NULL,
    published_at    timestamptz NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    raw_object_key  text,                               -- s3://masader-<env>/raw/...
    embedding       vector(384),                        -- paraphrase-multilingual-MiniLM-L12-v2
    origin_group_id bigint      REFERENCES origin_groups(id),
    first_hand      boolean,                            -- publisher of the statement or figure
    fts             tsvector GENERATED ALWAYS AS (to_tsvector('simple', title || ' ' || excerpt)) STORED,
    text_purged_at  timestamptz,
    UNIQUE (source_id, url_hash)                        -- idempotent ingest: ON CONFLICT DO NOTHING
);
CREATE INDEX items_published_idx    ON items (published_at);
CREATE INDEX items_content_hash_idx ON items (content_hash);
CREATE INDEX items_origin_idx       ON items (origin_group_id);
CREATE INDEX items_fts_idx          ON items USING gin (fts);
-- No ANN index in the core (ADR-04): search is an exact cosine scan inside the request's date window.
-- E1 builds this HNSW index only to compare against it; add it only past 1M items or a 500 ms p95:
-- CREATE INDEX items_embedding_hnsw ON items USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE statistics (                               -- World Bank series values (Tier 1, tier A)
    source_id   smallint NOT NULL REFERENCES sources(id),
    indicator   text     NOT NULL,                      -- e.g. 'NY.GDP.MKTP.CD'
    country     char(3)  NOT NULL,
    year        smallint NOT NULL,
    value       numeric,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    last_updated date,                                  -- the publisher's "as of" date
    PRIMARY KEY (indicator, country, year, retrieved_at)
);

-- ---------- requests and reports ----------
CREATE TABLE requests (
    id              text PRIMARY KEY,                   -- 'req_' + 12 base32 chars
    api_key_id      uuid        NOT NULL REFERENCES api_keys(id),
    idempotency_key text        NOT NULL,
    request_hash    bytea       NOT NULL,               -- sha256 of the normalised payload
    payload         jsonb       NOT NULL,               -- validated RequestIn (redacted)
    redactions      jsonb       NOT NULL DEFAULT '[]',
    clarification   jsonb,                              -- questions and answers
    status          request_status_t NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (api_key_id, idempotency_key)
);
CREATE INDEX requests_key_created_idx ON requests (api_key_id, created_at DESC);

CREATE TABLE reports (                                  -- Tier 4 once publication = 'published'
    id               text PRIMARY KEY,                  -- 'rep_' + 12 base32 chars
    request_id       text        NOT NULL REFERENCES requests(id),
    version          smallint    NOT NULL DEFAULT 1,
    status           request_status_t NOT NULL,
    publication      publication_t NOT NULL DEFAULT 'private',
    variant          text        NOT NULL DEFAULT 'B0',
    config_hash      text        NOT NULL,
    versions         jsonb       NOT NULL,              -- model and prompt version per role
    cache_key        bytea       NOT NULL,
    body             jsonb,                             -- ReportOut, immutable once published
    json_object_key  text,
    html_object_key  text,
    cost_usd         numeric(8,4) NOT NULL DEFAULT 0,
    duration_s       int,
    notes            jsonb       NOT NULL DEFAULT '[]', -- missing sources, fallbacks used
    approver_key_id  uuid        REFERENCES api_keys(id),
    approved_at      timestamptz,
    change_note      text,
    supersedes       text        REFERENCES reports(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_id, version)
);
CREATE INDEX reports_cache_key_idx   ON reports (cache_key) WHERE publication <> 'superseded';
CREATE INDEX reports_publication_idx ON reports (publication, created_at DESC);

CREATE TABLE report_stages (                            -- one row per pipeline stage; atomic claim + fenced commit
    report_id   text        NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    stage       stage_t     NOT NULL,
    status      stage_status_t NOT NULL DEFAULT 'pending',
    attempts    smallint    NOT NULL DEFAULT 0,
    started_at  timestamptz,
    finished_at timestamptz,
    output_ref  text,                                   -- pointer to stage output (table or S3 key)
    error       text,
    lease_owner text,                                   -- execution that holds the stage (atomic claim)
    lease_until timestamptz,                            -- claim expires; > hard time limit, < visibility timeout
    PRIMARY KEY (report_id, stage)
);

-- ---------- Tier 2: extracted claims ----------
CREATE TABLE claims (
    id                bigserial PRIMARY KEY,
    report_id         text        NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    text              text        NOT NULL,
    claim_type        claim_type_t NOT NULL,
    speaker           text,
    numbers           jsonb       NOT NULL DEFAULT '[]',
    dates             jsonb       NOT NULL DEFAULT '[]',
    entities          jsonb       NOT NULL DEFAULT '[]',
    extractor_version text        NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX claims_report_idx ON claims (report_id);

CREATE TABLE claim_passages (
    claim_id      bigint  NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    item_id       bigint  NOT NULL REFERENCES items(id),
    passage       text    NOT NULL,
    char_start    int,
    char_end      int,
    passage_found boolean NOT NULL,                     -- false = invented citation, claim dropped
    PRIMARY KEY (claim_id, item_id)
);

-- ---------- Tier 3: reviewed evidence ----------
CREATE TABLE claim_evidence (
    claim_id         bigint    NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    item_id          bigint    NOT NULL REFERENCES items(id),
    verdict          verdict_t NOT NULL,
    reason           text      NOT NULL,
    first_hand       boolean   NOT NULL,
    verifier_version text      NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, item_id, verifier_version)
);

CREATE TABLE claim_labels (
    claim_id           bigint      NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    rule_version       text        NOT NULL,
    label              label_t     NOT NULL,
    low_reliability    boolean     NOT NULL DEFAULT false,
    reliability_reason text        NOT NULL,
    review_status      review_status_t NOT NULL DEFAULT 'machine_checked',
    reviewed_by        uuid        REFERENCES api_keys(id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, rule_version)
);

CREATE TABLE report_assertions (                        -- output-validator result per checkable assertion
    report_id       text     NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    assertion_no    int      NOT NULL,
    section         text     NOT NULL,
    text            text     NOT NULL,
    cited_claim_ids bigint[] NOT NULL,
    result          text     NOT NULL CHECK (result IN ('pass', 'rewritten', 'removed')),
    fail_reason     text,
    PRIMARY KEY (report_id, assertion_no)
);

CREATE TABLE corrections (
    id          bigserial PRIMARY KEY,
    report_id   text        NOT NULL REFERENCES reports(id),
    claim_id    bigint      REFERENCES claims(id),
    reported_by uuid        NOT NULL REFERENCES api_keys(id),
    reason      text        NOT NULL,
    status      text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

-- ---------- AI accounting ----------
CREATE TABLE budgets (
    purpose      purpose_t    NOT NULL,
    period_start date         NOT NULL,                 -- first day of the budget period (the semester)
    cap_usd      numeric(8,2) NOT NULL,
    reserved_usd numeric(10,4) NOT NULL DEFAULT 0,      -- worst case of the calls in flight, settled after each
    spent_usd    numeric(10,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (purpose, period_start),
    CHECK (reserved_usd + spent_usd <= cap_usd)         -- the database refuses an overspend
);

CREATE TABLE ai_calls (
    id               bigserial PRIMARY KEY,
    report_id        text,
    purpose          purpose_t   NOT NULL,
    role             text        NOT NULL,               -- planner, extractor, verifier, writer, validator
    model            text        NOT NULL,
    prompt_version   text        NOT NULL,
    input_tokens     int         NOT NULL DEFAULT 0,
    output_tokens    int         NOT NULL DEFAULT 0,
    cache_read_tokens int        NOT NULL DEFAULT 0,
    cost_usd         numeric(8,5) NOT NULL DEFAULT 0,
    reserved_usd     numeric(8,5) NOT NULL DEFAULT 0,       -- worst case reserved in budgets before the call
    latency_ms       int,
    -- 'started' is written before the call; 'cancelled' = stopped by its stage; 'lost' = closed by the reaper
    outcome          text        NOT NULL CHECK (outcome IN ('started', 'ok', 'retry', 'fallback', 'invalid_json', 'timeout',
                                                             'error', 'refusal', 'cache_hit', 'cancelled', 'lost')),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_calls_created_idx ON ai_calls (created_at);
CREATE INDEX ai_calls_report_idx  ON ai_calls (report_id);

CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      uuid,
    action     text        NOT NULL,                    -- request.create, report.publish, key.revoke ...
    object_id  text,
    details    jsonb       NOT NULL DEFAULT '{}'
);

-- Published reports are immutable: corrections create a new version instead.
CREATE FUNCTION forbid_published_edit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.publication = 'published' AND (NEW.body IS DISTINCT FROM OLD.body OR NEW.version <> OLD.version) THEN
    RAISE EXCEPTION 'published report % is immutable; create a new version', OLD.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reports_immutable BEFORE UPDATE ON reports FOR EACH ROW EXECUTE FUNCTION forbid_published_edit();
