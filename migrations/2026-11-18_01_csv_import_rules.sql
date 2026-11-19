-- Build 1 (18/11/2026): rules-based CSV import. Applied automatically by
-- finance_import.ensure_schema() (called from dashboard _ensure_spending_schema); kept here for the record.
-- Backup taken first: ~/backups/ghost-postgres/ghost_db-pre-csv-import-20261118-232927.dump
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS norm_key TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS occurrence_idx INTEGER;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS dedupe_hash TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS rule_id BIGINT;
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_txn_date_description_amount_key;
CREATE UNIQUE INDEX IF NOT EXISTS transactions_dedupe_hash_key ON transactions (dedupe_hash);
CREATE INDEX IF NOT EXISTS transactions_norm_key_idx ON transactions (norm_key);
CREATE TABLE IF NOT EXISTS category_rules (
    id          BIGSERIAL PRIMARY KEY,
    match_type  TEXT NOT NULL CHECK (match_type IN ('exact','contains','regex')),
    pattern     TEXT NOT NULL,
    category    TEXT NOT NULL REFERENCES spending_categories(key) ON UPDATE CASCADE ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 100,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_type, pattern)
);
CREATE TABLE IF NOT EXISTS csv_import_formats (
    name      TEXT PRIMARY KEY,
    priority  INTEGER NOT NULL DEFAULT 100,
    enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    config    JSONB NOT NULL
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_rule_id_fkey') THEN
    ALTER TABLE transactions ADD CONSTRAINT transactions_rule_id_fkey
      FOREIGN KEY (rule_id) REFERENCES category_rules(id) ON DELETE SET NULL;
  END IF;
END $$;
-- One-off: blank the stored Ollama narration (Ollama removed).
UPDATE spending_summary SET narration='' WHERE narration IS DISTINCT FROM '';
