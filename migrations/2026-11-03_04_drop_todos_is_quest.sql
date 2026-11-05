-- Session 99b: drop the retired quest display flag (0 rows were flagged; code no longer reads or writes it).
ALTER TABLE todos DROP COLUMN IF EXISTS is_quest;
