-- Migration 0002: additions made during implementation (each one is listed in the deviations table).
SET search_path = masader, public;

-- the source register records whether a source is the official publisher of its statements
ALTER TABLE sources ADD COLUMN IF NOT EXISTS official boolean NOT NULL DEFAULT false;

-- corrections: who resolved them and how (FR10, IF-06)
ALTER TABLE corrections ADD COLUMN IF NOT EXISTS resolved_by uuid REFERENCES api_keys(id);
ALTER TABLE corrections ADD COLUMN IF NOT EXISTS resolution_note text;

-- per-report pipeline statistics (invented citations dropped, claims merged, assertions removed ...)
ALTER TABLE reports ADD COLUMN IF NOT EXISTS stats jsonb NOT NULL DEFAULT '{}';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS trace_id text;

-- which item a claim was extracted from (the claim_passages row also keeps it; this speeds up verify)
ALTER TABLE claims ADD COLUMN IF NOT EXISTS item_id bigint REFERENCES items(id);

-- the planner's search terms, stored with the request so collect uses the same plan
ALTER TABLE requests ADD COLUMN IF NOT EXISTS plan jsonb;

CREATE INDEX IF NOT EXISTS reports_status_idx ON reports (status) WHERE status IN ('queued', 'running', 'waiting_provider');
CREATE INDEX IF NOT EXISTS claim_evidence_claim_idx ON claim_evidence (claim_id);
