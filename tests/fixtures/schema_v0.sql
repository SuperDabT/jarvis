-- Frozen schema of a Jarvis database before versioned migrations (user_version 0),
-- as written by db.connect() at commit 110cb8d: SCHEMA plus the add-column MIGRATIONS.
-- Never edit: tests upgrade this to prove existing databases migrate safely.
CREATE TABLE IF NOT EXISTS etags (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY,
    title TEXT,
    state TEXT,
    labels TEXT,
    milestone_number INTEGER,
    body TEXT,
    html_url TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS milestones (
    number INTEGER PRIMARY KEY,
    title TEXT,
    state TEXT,
    description TEXT,
    due_on TEXT,
    open_issues INTEGER,
    closed_issues INTEGER,
    html_url TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    message TEXT,
    author TEXT,
    committed_at TEXT,
    html_url TEXT
);

CREATE TABLE IF NOT EXISTS repo_files (
    path TEXT PRIMARY KEY,
    sha TEXT,
    content TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT,
    resource TEXT,
    status INTEGER,
    changed INTEGER
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT CHECK (type IN ('electrical', 'research')),
    title TEXT,
    why TEXT,
    source_url TEXT,
    source_ref TEXT,
    check_id TEXT,
    paper_id TEXT,
    claude_prompt TEXT,
    status TEXT CHECK (status IN ('active', 'done', 'auto_resolved')) DEFAULT 'active',
    fingerprint TEXT UNIQUE NOT NULL,
    feedback TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facts_hash TEXT NOT NULL,
    headline TEXT,
    sentences_json TEXT,
    source TEXT CHECK (source IN ('model', 'template')),
    model TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS briefings_hash ON briefings (facts_hash, id);

-- Latest electrical findings (phase 4), one row per check_id, plus the cached
-- model wording for that exact finding (keyed by finding_hash).
CREATE TABLE IF NOT EXISTS electrical_findings (
    check_id TEXT PRIMARY KEY,
    finding_hash TEXT,
    status TEXT,
    title TEXT,
    why TEXT,
    numbers_json TEXT,
    model_hash TEXT,
    model_result TEXT,
    model_title TEXT,
    model_why TEXT,
    model_issue INTEGER,
    model TEXT,
    updated_at TEXT
);

-- Phase 5: the fast loop's heartbeat and change-detection state (one row, id = 1).
-- pid/started_at/interval_s/next_tick_at belong to a running `jarvis watch` and are
-- cleared when it exits; the hashes are shared by `jarvis watch --once`.
CREATE TABLE IF NOT EXISTS daemon_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER,
    started_at TEXT,
    last_tick_at TEXT,
    last_change_at TEXT,
    last_error TEXT,
    interval_s INTEGER,
    next_tick_at TEXT,
    hardware_hash TEXT,
    facts_hash TEXT,
    model_pending INTEGER DEFAULT 0
);

-- Phase 6: the research loop.
-- Papers seen on arXiv (and their cached embedding for one embed model).
CREATE TABLE IF NOT EXISTS research_papers (
    paper_id TEXT PRIMARY KEY,
    title TEXT,
    authors_json TEXT,
    abstract TEXT,
    published TEXT,
    url TEXT,
    categories TEXT,
    embed_model TEXT,
    embedding BLOB,
    fetched_at TEXT
);

-- Every (paper, issue) pair the model judged, accepted or not, so it is never
-- judged twice. Model errors are not stored.
CREATE TABLE IF NOT EXISTS research_seen (
    paper_id TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    verdict TEXT,
    confidence TEXT,
    applies_to TEXT,
    why TEXT,
    reason TEXT,
    similarity REAL,
    judged_at TEXT,
    model TEXT,
    PRIMARY KEY (paper_id, issue_number)
);

-- Model-proposed search queries per issue, keyed by a hash of the issue text.
CREATE TABLE IF NOT EXISTS research_queries (
    issue_number INTEGER PRIMARY KEY,
    key TEXT,
    worthy INTEGER,
    reason TEXT,
    queries_json TEXT,
    model TEXT,
    created_at TEXT
);

-- arXiv answers per search string (paper ids in rank order), reused for a while so
-- re-runs do not ask arXiv again.
CREATE TABLE IF NOT EXISTS research_searches (
    query TEXT PRIMARY KEY,
    paper_ids TEXT,
    fetched_at TEXT
);

-- Library release baselines: the newest tag seen per repo.
CREATE TABLE IF NOT EXISTS research_releases (
    repo TEXT PRIMARY KEY,
    tag TEXT,
    url TEXT,
    source TEXT,
    baseline_at TEXT,
    updated_at TEXT
);
-- Chat: questions asked in the window and Jarvis's answers, per window session.
-- sources_json (assistant rows): {"sources": [...], "notes": [...], "unverified_numbers": [...]}.
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT CHECK (role IN ('user', 'assistant')),
    content TEXT,
    sources_json TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages (session_id, id);

-- Small key/value state for sync bookkeeping, e.g. how far back the commit
-- history has been fetched ("commits_since").
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

ALTER TABLE daemon_state ADD COLUMN last_research_at TEXT;
ALTER TABLE daemon_state ADD COLUMN next_research_at TEXT;
ALTER TABLE daemon_state ADD COLUMN research_summary TEXT;
