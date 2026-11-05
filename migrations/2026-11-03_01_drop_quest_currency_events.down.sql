-- Reverse: recreate the table, then \copy the CSV back in:
--   \copy quest_currency_events from '~/backups/ghost-postgres/quest_currency_events-20261103.csv' csv header
CREATE TABLE public.quest_currency_events (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    item_id text NOT NULL,
    amount integer NOT NULL,
    event_type text DEFAULT 'base' NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE INDEX quest_currency_created_at_idx ON public.quest_currency_events USING btree (created_at);
