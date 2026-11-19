-- Session 104: sent marker on decision cards.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ;
-- Backfill: every card pending at deploy time counts as already sent (nothing floods the phone).
UPDATE decisions SET pushed_at = now() WHERE status = 'pending' AND pushed_at IS NULL;
