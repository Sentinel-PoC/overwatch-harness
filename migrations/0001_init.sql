-- overwatch-harness ledger schema v0.1 (OPS-197)
-- Audit-trail source of truth for all Telegram → Claude routing decisions.
-- Applied by deploy/install.sh on first install; subsequent migrations
-- additive only (NEVER drop / rewrite existing tables in v0.x).

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- operator: identity normalization across channels
-- A Telegram chat 12345 = operator op:tg:12345.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operator (
    operator_id        TEXT PRIMARY KEY,                 -- "op:tg:12345"
    channel_kind       TEXT NOT NULL CHECK (channel_kind IN ('telegram','discord','direct')),
    channel_user_id    TEXT NOT NULL,                    -- chat_id / user_id from the channel
    display_name       TEXT,
    plane_actor_id     TEXT,                             -- Plane user uuid (optional)
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (channel_kind, channel_user_id)
);

CREATE INDEX IF NOT EXISTS idx_operator_channel
    ON operator(channel_kind, channel_user_id);

-- ---------------------------------------------------------------------------
-- engagement: a logical conversation thread that may span N Claude sessions
-- Default: 30-minute idle window collapses an engagement; new message after
-- idle = new engagement. Operator can also force /end + /new.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engagement (
    engagement_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id            TEXT NOT NULL REFERENCES operator(operator_id),
    started_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_activity_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at               TEXT,                          -- NULL = active
    mode                   TEXT NOT NULL DEFAULT 'continuous'
                              CHECK (mode IN ('oneshot','continuous','paused','ended')),
    current_session_id     INTEGER,                       -- FK to session(session_id), NULL when between sessions
    plane_issue_id         TEXT,                          -- Plane issue uuid for this engagement's audit
    plane_issue_seq        INTEGER,                       -- human-readable sequence id (e.g. OPS-NNN)
    summary                TEXT                           -- short operator-visible label
);

CREATE INDEX IF NOT EXISTS idx_engagement_operator_active
    ON engagement(operator_id, ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_engagement_operator_recent
    ON engagement(operator_id, last_activity_at DESC);

-- ---------------------------------------------------------------------------
-- session: one row per `claude -p` invocation
-- claude_session_id = the UUID Claude returns in its first stream-json chunk.
-- parent_session_id is set when this session was a --resume of another.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
    session_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    engagement_id           INTEGER NOT NULL REFERENCES engagement(engagement_id),
    claude_session_id       TEXT,                         -- Claude UUID; NULL until first stream chunk
    parent_session_id       INTEGER REFERENCES session(session_id),
    started_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at                TEXT,
    status                  TEXT NOT NULL DEFAULT 'spawning'
                              CHECK (status IN ('spawning','running','completed','failed','interrupted','quota_exhausted')),
    prompt_excerpt          TEXT NOT NULL,                -- first 500 chars of input prompt
    response_excerpt        TEXT,                         -- first 500 chars of final output
    exit_code               INTEGER,
    langfuse_trace_id       TEXT,                         -- if discoverable from spawned session's hook output
    max_quota_reset_window  TEXT,                         -- ISO timestamp of next quota reset on quota_exhausted
    duration_ms             INTEGER
);

CREATE INDEX IF NOT EXISTS idx_session_engagement
    ON session(engagement_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_status_active
    ON session(status) WHERE status IN ('spawning','running');

-- ---------------------------------------------------------------------------
-- message: every inbound message lands here BEFORE classification
-- Idempotency key: (channel_kind, channel_msg_id) — Telegram update_id
-- replays don't create duplicate Plane issues.
-- Replay-on-restart: daemon scans for status='received'/'classifying'
-- on boot and resumes processing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message (
    message_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id            TEXT NOT NULL REFERENCES operator(operator_id),
    engagement_id          INTEGER REFERENCES engagement(engagement_id),
    session_id             INTEGER REFERENCES session(session_id),
    channel_kind           TEXT NOT NULL,
    channel_msg_id         TEXT NOT NULL,                 -- Telegram update_id (string for portability)
    received_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    body                   TEXT NOT NULL,
    reply_to_msg_id        TEXT,                          -- Telegram reply_to_message_id; NULL if not a reply
    classifier_route       TEXT CHECK (classifier_route IN
                              ('slash_command','status_query','routine_action','spec_request','security_event','operator_review','unknown')),
    classifier_version     TEXT,                          -- prompt+model+threshold fingerprint
    prompt_hash            TEXT,                          -- sha256 of the classifier prompt
    reply_text             TEXT,                          -- what the daemon told the operator
    status                 TEXT NOT NULL DEFAULT 'received'
                              CHECK (status IN ('received','classifying','routed','failed','dead_letter','done')),
    error_summary          TEXT,                          -- if status IN ('failed','dead_letter')
    UNIQUE (channel_kind, channel_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_message_operator_recent
    ON message(operator_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_engagement
    ON message(engagement_id, received_at);
CREATE INDEX IF NOT EXISTS idx_message_status_pending
    ON message(status) WHERE status IN ('received','classifying');

-- ---------------------------------------------------------------------------
-- routing_failures: dead-letter queue for unrecoverable classifier errors
-- Written when a message can't be classified after retry; operator inspects.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routing_failures (
    failure_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        INTEGER NOT NULL REFERENCES message(message_id),
    failed_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    failure_reason    TEXT NOT NULL,
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    last_error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_routing_failures_message
    ON routing_failures(message_id);

-- ---------------------------------------------------------------------------
-- heartbeat: liveness pings for the watchdog
-- Daemon writes one row every 60s. External check fires alert if stale > 5m.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heartbeat (
    heartbeat_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    pid               INTEGER NOT NULL,
    state             TEXT NOT NULL CHECK (state IN ('starting','degraded','healthy','quota_throttled','shutting_down')),
    in_flight_count   INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

-- Cap at last 1440 rows (~24h at 60s cadence) via daemon-side prune;
-- index for the most-recent-row query
CREATE INDEX IF NOT EXISTS idx_heartbeat_recent
    ON heartbeat(ts DESC);

-- ---------------------------------------------------------------------------
-- schema_version: lets future migrations know what's already applied
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version           INTEGER PRIMARY KEY,
    applied_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    description       TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, description)
    VALUES (1, 'initial schema: operator, engagement, session, message, routing_failures, heartbeat');
