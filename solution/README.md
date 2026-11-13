# Interaction Ledger & Reporting Pipeline (Assignment A)

## System Architecture

```
                       ┌─────────────────────────┐
                       │  Input Drops (CSV/JSON) │
                       └────────────┬────────────┘
                                    │
               ┌────────────────────┴───────────────────┐
               ▼                                        ▼
    ┌──────────────────────┐                ┌──────────────────────┐
    │  Lead CSV Pipeline   │                │ Event JSONL Pipeline │
    │  - Header Aliases    │                │  - Deduplication     │
    │  - Schema Validation │                │  - PII Sanitization  │
    │  - Rejections Log    │                │  - Conflict Check    │
    │  - PII Encrypt (GCM) │                │  - Call Linking      │
    │  - Duplicate Merges  │                │  - Quarantine Table  │
    └──────────┬───────────┘                └──────────┬───────────┘
               │                                        │
               └────────────────────┬───────────────────┘
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        SQLite Database Storage                         │
│                                                                        │
│ • leads (workflow_id, phone_hmac UNIQUE, phone_enc, pii_attributes_enc,│
│          attributes_json, batch_id, duplicate_count)                   │
│ • interaction_ledger (seq, event_id UNIQUE, append-only triggers)      │
│ • call_phone_links (call_id -> phone_hmac, is_conflicted)              │
│ • call_phone_conflicts (audit log for conflicting event phones)        │
│ • unresolved_events (quarantined orphans & unlinked overrides)         │
│ • rejections (workflow_id, batch_id, row_number, reason)               │
│ • ingested_files (relative_path, file_sha256, transaction audits)      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        SQL Reporting Engine                            │
│  tmp_call_facts (CTEs & window functions)                              │
│  - Filter unknown dispositions before effective ranking                │
│  - Overrides strictly prioritize over sets regardless of time          │
│  - Latest override by occurred_at wins                                 │
│  tmp_lead_summary (Lead bucket ranking & current disposition)          │
│  Output: batch_summary, daily_overview, lead_report, rejections, DQ    │
└────────────────────────────────────────────────────────────────────────┘
```

## Layout

- `solution/cli.py` — CLI entry point (`python solution/cli.py ...`)
- `solution/au_pipeline/` — Pipeline package (`python -m solution.au_pipeline ...`)
  - `config.py` — dynamic loading of workflow schemas and `dispositions.csv`
  - `crypto.py` — HKDF key separation, keyed HMAC-SHA256 phone tokens, AES-256-GCM PII encryption
  - `normalizers.py` — header, phone, date, decimal, enum, and IST timestamp normalization
  - `db.py` — SQLite schema, indexes, append-only triggers, and quarantine tables
  - `ingest.py` — idempotent, transactional lead CSV & event JSONL ingestion with conflict detection and resolution
  - `report.py` — SQL-based report aggregation and CSV/JSON writers
- `solution/tests/` — 8 modular unit and integration test suites:
  - `test_config.py`
  - `test_normalizers.py`
  - `test_crypto.py`
  - `test_leads.py`
  - `test_events.py`
  - `test_dispositions.py`
  - `test_reports.py`
  - `test_idempotency.py`

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Ingest drops incrementally
python solution/cli.py ingest data/drop_1
python solution/cli.py ingest data/drop_2

# Generate all 5 report files
python solution/cli.py report --out reports/
```

Run test suite:

```bash
python3 -m unittest discover -s solution/tests -v
# or
pytest
```
