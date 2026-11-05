-- Session 99: drop the retired quest currency ledger. Unreferenced in code since 19/10/2026.
-- Data (9 rows) exported to ~/backups/ghost-postgres/quest_currency_events-20261103.csv before this runs.
DROP TABLE IF EXISTS public.quest_currency_events;
