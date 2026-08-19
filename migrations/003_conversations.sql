-- [claude] The conversation index the thread endpoints need.
--
-- Why this exists alongside the LangGraph checkpointer
-- ----------------------------------------------------
-- The checkpointer already stores every conversation: `checkpoints`,
-- `checkpoint_blobs` and `checkpoint_writes` are created by
-- AsyncPostgresSaver.setup() and hold the messages. It cannot, however,
-- answer either question the front end actually asks:
--
--   "which conversations does this employee have?"
--       Its tables are keyed by thread_id with no notion of an owner, and
--       listing across all threads would page through other employees'
--       conversations to find one user's.
--
--   "does this employee own this thread?"
--       There is nothing to check against. Thread ids arrive from a
--       browser, so without an ownership record the only thing standing
--       between one employee and another's conversation history is the
--       namespacing in Principal.thread_key() — which is real, but is a
--       single function that a future caller could route around.
--
-- So this table is the index and the ownership record. The checkpointer
-- keeps the messages; this keeps who they belong to, in a row that can be
-- checked in one statement.
--
--   psql -h <host> -U <owner> -d <database> -f migrations/003_conversations.sql
--
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS conversations (
    -- The checkpointer's thread_id. Namespaced per subject by
    -- Principal.thread_key(), so it is never the raw value a client sent.
    thread_key      text PRIMARY KEY,

    -- The authenticated subject from the JWT. This is the ownership record.
    subject         text        NOT NULL,

    -- The id as the client knows it, echoed back so the front end can use
    -- its own identifiers without ever learning the namespaced key.
    thread_id       text        NOT NULL,

    -- First question of the conversation, trimmed. A display label only.
    title           text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- Turns, not messages: incremented once per completed exchange.
    turn_count      integer     NOT NULL DEFAULT 0,

    -- [claude] One conversation belongs to one subject, and a thread_id is
    -- only unique within a subject — two employees may both call a thread
    -- "today". thread_key already encodes both, but stating it as a
    -- constraint means a bug in thread_key() surfaces as a conflict here
    -- rather than as one user's turn appended to another's conversation.
    CONSTRAINT conversations_subject_thread_unique UNIQUE (subject, thread_id)
);

-- The listing query: this subject's conversations, most recent first.
CREATE INDEX IF NOT EXISTS conversations_subject_updated_idx
    ON conversations (subject, updated_at DESC);

COMMIT;
