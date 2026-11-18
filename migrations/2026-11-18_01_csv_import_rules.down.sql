-- Reverses 2026-11-18_01. Re-adding the old unique constraint FAILS if two identical same-day
-- same-amount rows were imported since (that is the point of the change) — resolve those first.
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_rule_id_fkey;
DROP TABLE IF EXISTS category_rules;
DROP TABLE IF EXISTS csv_import_formats;
DROP INDEX IF EXISTS transactions_dedupe_hash_key;
DROP INDEX IF EXISTS transactions_norm_key_idx;
ALTER TABLE transactions DROP COLUMN IF EXISTS rule_id, DROP COLUMN IF EXISTS dedupe_hash,
  DROP COLUMN IF EXISTS occurrence_idx, DROP COLUMN IF EXISTS norm_key;
ALTER TABLE transactions ADD CONSTRAINT transactions_txn_date_description_amount_key UNIQUE (txn_date, description, amount);
