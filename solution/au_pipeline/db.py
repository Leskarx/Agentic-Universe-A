from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DispositionInfo

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    phone_hmac BLOB NOT NULL,
    phone_enc BLOB NOT NULL,
    external_ref TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    pii_attributes_enc BLOB NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workflow_id, phone_hmac)
);

CREATE INDEX IF NOT EXISTS idx_leads_wf_ref ON leads (workflow_id, external_ref);
CREATE INDEX IF NOT EXISTS idx_leads_wf_batch ON leads (workflow_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_leads_wf_phone ON leads (workflow_id, phone_hmac);

CREATE TABLE IF NOT EXISTS interaction_ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    occurred_at_ist TEXT NOT NULL,
    occurred_date_ist TEXT NOT NULL,
    phone_hmac BLOB,
    disposition_code TEXT,
    payload_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_wf_phone ON interaction_ledger (workflow_id, phone_hmac);
CREATE INDEX IF NOT EXISTS idx_ledger_wf_call ON interaction_ledger (workflow_id, call_id);
CREATE INDEX IF NOT EXISTS idx_ledger_type_wf ON interaction_ledger (event_type, workflow_id);
CREATE INDEX IF NOT EXISTS idx_ledger_occurred ON interaction_ledger (occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_ledger_call_disp ON interaction_ledger (workflow_id, call_id, event_type, occurred_at_utc, seq);

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE ON interaction_ledger
BEGIN
    SELECT RAISE(ABORT, 'interaction_ledger is append-only: updates are forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete
BEFORE DELETE ON interaction_ledger
BEGIN
    SELECT RAISE(ABORT, 'interaction_ledger is append-only: deletes are forbidden');
END;

CREATE TABLE IF NOT EXISTS call_phone_links (
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    phone_hmac BLOB NOT NULL,
    is_conflicted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, call_id)
);

CREATE INDEX IF NOT EXISTS idx_call_links_wf_phone ON call_phone_links (workflow_id, phone_hmac);

CREATE TABLE IF NOT EXISTS call_phone_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    existing_phone_hmac BLOB NOT NULL,
    conflicting_phone_hmac BLOB NOT NULL,
    event_id TEXT NOT NULL,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unresolved_events (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    phone_hmac BLOB,
    unresolved_reason TEXT NOT NULL,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES interaction_ledger(event_id)
);

CREATE INDEX IF NOT EXISTS idx_unresolved_wf_status ON unresolved_events (workflow_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_unresolved_call ON unresolved_events (workflow_id, call_id);
CREATE INDEX IF NOT EXISTS idx_unresolved_phone ON unresolved_events (workflow_id, phone_hmac);

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rejections_sort ON rejections (workflow_id, batch_id, row_number);

CREATE TABLE IF NOT EXISTS ingested_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    file_kind TEXT NOT NULL CHECK (file_kind IN ('leads', 'events')),
    workflow_id TEXT,
    batch_id TEXT,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    duplicates_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    events_total INTEGER NOT NULL DEFAULT 0,
    malformed_events INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL,
    UNIQUE (rel_path, file_sha256)
);
"""


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -64000;")
    return conn


def init_db(conn: sqlite3.Connection, dispositions: dict[tuple[str, str], DispositionInfo]) -> None:
    conn.executescript(SCHEMA_SQL)
    rows = [
        (d.workflow_id, d.code, d.label, 1 if d.is_terminal else 0, 1 if d.is_success else 0)
        for d in dispositions.values()
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO dispositions (workflow_id, code, label, is_terminal, is_success)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id, code) DO UPDATE SET
                label = excluded.label,
                is_terminal = excluded.is_terminal,
                is_success = excluded.is_success
            """,
            rows,
        )
