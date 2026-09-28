-- ============================================================
-- Citizen Development Intelligence Platform
-- Migration v4 -> v5
--
-- Adds what the FastAPI backend needs:
--   1. complaint_id_seq                 -> IDs like REQ-000001
--   2. complaints.severity_source       -> 'ai' | 'fallback' (fallback rows can be re-processed later)
--   3. complaints.client_timestamp      -> timestamp sent by the client (created_at stays server time)
--   4. hotspots.population_imputed      -> TRUE when Census population was missing/0 and a median was used
--   5. complaint_status_history         -> audit trail for PATCH /complaints/{id}/status (incl. note)
--
-- Safe to re-run: every statement is IF NOT EXISTS / idempotent.
-- Run it once in the Supabase SQL editor (whole file at once is fine).
-- ============================================================

BEGIN;

-- 1. Complaint ID sequence ------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS complaint_id_seq START WITH 1 INCREMENT BY 1;

-- 2. Where severity/urgency came from ------------------------------------
--    'ai'       = produced by Person 1's AI (Flow A webhook, or Flow B pre-processed)
--    'fallback' = AI call failed/timed out; defaults severity 3 / urgency medium were used
--    NULL       = legacy rows inserted before v5 (the TEST-00x scaffolding)
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS severity_source VARCHAR(20);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'complaints_severity_source_check') THEN
        ALTER TABLE complaints
            ADD CONSTRAINT complaints_severity_source_check
            CHECK (severity_source IS NULL OR severity_source IN ('ai', 'fallback'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_complaints_severity_source
    ON complaints (severity_source) WHERE severity_source = 'fallback';

-- 3. Client-supplied timestamp (created_at remains server time) ----------
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS client_timestamp TIMESTAMPTZ;

-- 4. Population imputation flag on hotspots ------------------------------
ALTER TABLE hotspots ADD COLUMN IF NOT EXISTS population_imputed BOOLEAN DEFAULT FALSE;

-- 5. Status history ------------------------------------------------------
CREATE TABLE IF NOT EXISTS complaint_status_history (
    id            SERIAL PRIMARY KEY,
    complaint_id  VARCHAR(20) NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
    old_status    VARCHAR(20),
    new_status    VARCHAR(20) NOT NULL,
    updated_by    VARCHAR(100),
    note          TEXT,
    changed_at    TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_status_history_complaint
    ON complaint_status_history (complaint_id, changed_at);

-- Helpful for the active-only filter used by every recompute
-- (active = complaint_status IN ('open','in_progress')).
CREATE INDEX IF NOT EXISTS idx_complaints_active_lgd_cat
    ON complaints (resolved_lgd_code, category)
    WHERE complaint_status IN ('open', 'in_progress');

COMMIT;

-- ------------------------------------------------------------
-- Verify (expected: 5 rows, one per new object)
-- ------------------------------------------------------------
SELECT 'complaint_id_seq' AS object, EXISTS (SELECT 1 FROM pg_class WHERE relname = 'complaint_id_seq') AS present
UNION ALL SELECT 'complaints.severity_source', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'complaints' AND column_name = 'severity_source')
UNION ALL SELECT 'complaints.client_timestamp', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'complaints' AND column_name = 'client_timestamp')
UNION ALL SELECT 'hotspots.population_imputed', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'hotspots' AND column_name = 'population_imputed')
UNION ALL SELECT 'complaint_status_history', EXISTS (SELECT 1 FROM pg_class WHERE relname = 'complaint_status_history');
