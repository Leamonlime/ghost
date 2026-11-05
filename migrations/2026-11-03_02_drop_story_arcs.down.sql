CREATE TABLE public.story_arcs (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL,
    name text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
