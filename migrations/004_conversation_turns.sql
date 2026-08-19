-- [claude] Provenance kept alongside the conversation.
--
-- The queries behind an answer are captured per request and returned in the
-- chat response, which is what makes a number checkable at the moment it is
-- given. Reopening a conversation lost them: the checkpointer stores the
-- messages and knows nothing about SQL, so a replayed answer was back to
-- being a number you had to trust.
--
-- One row per completed turn, holding the provenance for that turn. The
-- messages stay in the checkpointer; only what it cannot represent lives
-- here, which is the same split `conversations` already makes for ownership.
--
--   psql -h <host> -U <owner> -d <database> -f migrations/004_conversation_turns.sql
--
-- Idempotent: safe to re-run. Requires 003.

BEGIN;

CREATE TABLE IF NOT EXISTS conversation_turns (
    thread_key  text        NOT NULL,

    -- 1-based, matching `conversations.turn_count` after that turn landed.
    -- Replay pairs the Nth stored turn with the Nth assistant message.
    turn_index  integer     NOT NULL,

    -- The QueryRecord list as returned by the API: sql, question,
    -- rows_available, truncated, refused. JSONB rather than columns because
    -- a turn runs zero or many queries, and the shape is already defined by
    -- app/sql/provenance.py.
    provenance  jsonb       NOT NULL DEFAULT '[]'::jsonb,

    created_at  timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (thread_key, turn_index),

    -- [claude] Cascade, so deleting a conversation cannot leave provenance
    -- behind. Those rows contain the SQL asked of the CRM; an orphaned set
    -- would be unreachable through the API and still on disk, which is the
    -- shape of every retention problem.
    CONSTRAINT conversation_turns_thread_fk
        FOREIGN KEY (thread_key) REFERENCES conversations (thread_key)
        ON DELETE CASCADE
);

COMMIT;
