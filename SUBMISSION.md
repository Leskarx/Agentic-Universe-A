# Submission

## Candidate
- **Name:** `Gouri Shankar Konwar`
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~10h

## How to run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generate synthetic data (small or large)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# Incremental ingest and report generation
python solution/cli.py ingest data/drop_1
python solution/cli.py ingest data/drop_2
python solution/cli.py report --out reports/

# Run tests (28 unit & integration tests across 8 test modules)
python -m unittest discover -s solution/tests -v
pytest
```

Optional flags & environment variables:
- `--db <path>` / `AU_DB_PATH` (default: `au_ledger.db`)
- `--schemas-dir <path>` / `AU_SCHEMAS_DIR` (default: `assignment-a-data/schemas`)
- `AU_PII_KEY` (32-byte hex or URL-safe base64 key; if unset on a new DB, a 256-bit key is generated with `0600` permissions at `<db_path>.key`)

## Design

### Architecture Overview
The pipeline lives in [`solution/au_pipeline/`](solution/au_pipeline/) and operates on SQLite (`WAL` mode, `NORMAL` sync, memory temp store):

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

1. **Dynamic schema & catalogue loader ([`config.py`](solution/au_pipeline/config.py)):** Loads all `*.json` workflow lead schemas and `dispositions.csv` dynamically at runtime.
2. **Normalizers ([`normalizers.py`](solution/au_pipeline/normalizers.py)):** Pure functions for header matching, Indian mobile normalization (`+91XXXXXXXXXX`), day-first date parsing, currency/decimal parsing (`Decimal` + `ROUND_HALF_UP`), case-insensitive enums, and UTC/offset/naive-IST timestamp conversion.
3. **PII protection ([`crypto.py`](solution/au_pipeline/crypto.py)):** Derives separate 256-bit AES-GCM and HMAC-SHA256 keys from a 32-byte master key via HKDF-SHA256.
   - Schema-driven encryption: every field marked `"pii": true` (e.g. `customer_name`, and any future PII field) is stored inside an AES-256-GCM encrypted JSON payload (`pii_attributes_enc`).
   - `phone` has a dedicated `phone_enc` for masked report rendering and `phone_hmac` (`HMAC-SHA256(hmac_key, canonical_phone)`) for indexed $O(1)$ equality matching across leads and events without plaintext exposure.
   - Raw `phone` and `customer_name` strings are stripped from incoming event payloads before writing to `interaction_ledger`.
4. **Database schema ([`db.py`](solution/au_pipeline/db.py)):**
   - `leads`: `(workflow_id, phone_hmac)` `UNIQUE`; immutable `batch_id`; `duplicate_count` tracks duplicate rows merged; non-PII optional attributes stored in `attributes_json` and merged via SQLite `json_patch`.
   - `interaction_ledger`: append-only event ledger protected against mutation by SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers.
   - `call_phone_links`: maps `(workflow_id, call_id) -> phone_hmac`. Detects conflicting phone mappings for the same call and flags `is_conflicted = 1`, logging to `call_phone_conflicts`.
   - `unresolved_events`: tracks quarantined orphan events (events arriving before leads) and unlinked overrides. When matching leads or call links arrive, matching events are marked `resolved_at = ISO_TIMESTAMP`.
   - `ingested_files`: stores file-level cryptographic hashes `(rel_path, file_sha256)` within the same atomic transaction as the file's data.
5. **SQL-based reporting ([`report.py`](solution/au_pipeline/report.py)):** Computes `tmp_call_facts` and `tmp_lead_summary` using SQLite CTEs and `ROW_NUMBER()` window functions. Filtered dispositions join against `dispositions.csv` before ranking, ensuring unknown disposition codes never shadow valid dispositions.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Dedicated `unresolved_events` table | Explicit quarantine tracking for events arriving before leads or unlinked overrides; resolved idempotently when matching leads/call links arrive | Re-scanning the entire interaction ledger on every ingest |
| Schema-driven PII encryption (`pii_attributes_enc`) | Protects any field marked `"pii": true` in an encrypted JSON payload without altering database table DDL for future schemas | Hardcoding columns like `customer_name_enc` |
| Keyed `HMAC-SHA256` for `phone_hmac` | 10-digit Indian mobile numbers ($4 \times 10^9$ possibilities) have a small search space susceptible to brute-force dictionary attacks if hashed with unsalted SHA-256; keyed HMAC prevents offline enumeration | Unsalted SHA-256 or deterministic AES-SIV |
| Filter unknown dispositions *before* `ROW_NUMBER()` ranking | Ensures invalid overrides (e.g. `INVALID_CODE`) are discarded before selecting effective disposition, so a valid `disposition.set` (e.g. `renewed`) is not shadowed | Ranking all events and nullifying unknown codes afterwards |
| Detect conflicting call-to-phone mappings | If two events report different phones for the same `call_id`, flag `is_conflicted = 1` and quarantine call events to prevent misattributing calls to the wrong customer | `INSERT OR IGNORE` which silently drops the second phone mapping |
| Track `duplicate_count` and merge only non-null values via `json_patch` | Preserves original `batch_id`, tracks duplicate frequency, and applies defaults (`preferred_language -> en`) at read time rather than burning them into stored JSON | Writing defaults into the JSON on row 1 which would falsely block updates |
| Idempotency by `(rel_path, file_sha256)` in `ingested_files` | Atomic per-file transaction ensures re-runs do not re-process data or double-count `data_quality.json` metrics | Deduplication only at the event level |

## Failure modes considered
- **Re-running `ingest` on an already ingested drop:** Skipped atomically via `(rel_path, file_sha256)` check; produces byte-identical reports.
- **Crash mid-file during `ingest`:** File records, rejections, unresolved entries, and `ingested_files` commit inside a single atomic SQLite transaction (`with conn:`), rolling back cleanly on failure.
- **Events arriving before leads:** Held in `unresolved_events` with `unresolved_reason = 'lead_not_found'`; automatically resolved when the lead CSV arrives in a later drop.
- **Overrides arriving before call initiation:** Held with `unresolved_reason = 'call_unlinked'`; resolved when `call.initiated` arrives with the phone number.
- **Conflicting call-to-phone mappings:** Flagged in `call_phone_links` as `is_conflicted = 1` and logged in `call_phone_conflicts`; quarantined from call facts.
- **Known gaps / not handled:**
  - In-place modification of an existing file path with changed SHA-256 is treated as a new upload rather than rolling back prior rows.
  - Concurrent multi-process writers to the same SQLite DB rely on SQLite file locking (a queue or PostgreSQL would be used for multi-node production scale).

## Testing
Organized into 8 dedicated test modules under [`solution/tests/`](solution/tests/) (28 total tests, all passing):
- [`test_config.py`](solution/tests/test_config.py): Schema loading, reference field extraction, header alias mappings, catalogue validation (including `refused` in `policy_feedback`), and defaults application.
- [`test_normalizers.py`](solution/tests/test_normalizers.py): Header normalization, all 6 Indian phone formats + invalid numbers, day-first date normalization, leap/non-leap validation, decimal/currency parsing, enum normalization, and UTC/offset/naive-IST conversion.
- [`test_crypto.py`](solution/tests/test_crypto.py): HKDF key derivation, AES-256-GCM encryption/decryption of strings and JSON dictionaries, HMAC consistency, and key file creation.
- [`test_leads.py`](solution/tests/test_leads.py): Schema-order rejections, blank row skipping, non-rejected field warnings, duplicate merging with immutable `batch_id`, and `duplicate_count` incrementing.
- [`test_events.py`](solution/tests/test_events.py): Malformed JSON, cross-drop deduplication, `unresolved_events` quarantine and resolution, and conflicting call-to-phone mapping detection.
- [`test_dispositions.py`](solution/tests/test_dispositions.py): Priority of overrides over sets regardless of timestamp, latest override ordering by `occurred_at_utc`, unknown disposition filtering prior to ranking, and terminal/success flags.
- [`test_reports.py`](solution/tests/test_reports.py): End-to-end report generation verifying exact column headers, formatting (e.g. 4-decimal connectivity rate `0.3333`), masked phones (`+91******0001`), lead bucket classifications, and data quality counters.
- [`test_idempotency.py`](solution/tests/test_idempotency.py): Zero-change re-ingestion, append-only trigger enforcement on `interaction_ledger`, and byte-level inspection confirming zero plaintext PII in database files.

## Results

### Small (`--seed 42 --scale small`)
- Ingest `drop_1`: `0.34s` | Ingest `drop_2`: `0.30s` | Re-ingest `drop_1` (no-op): `0.01s` | Report: `0.10s`
- Verified byte-for-byte identical reports (`diff -ru`) across incremental, re-ingested, and all-at-once runs.
- `lead_rows_total`: 2,057 (`108` rejected, `57` merged duplicates -> `1,892` unique leads)
- `events_total`: 8,745 (`8` malformed, `237` duplicate dropped, `462` orphan, `18` unknown disposition)

### Large (`--seed 42 --scale large` — stretch goal)
Measured on local Linux workstation (Python 3.14 / SQLite 3.53):
- Ingest `drop_1` (308,063 event lines + 4 lead files): **`40.00s`**
- Ingest `drop_2` (324,539 event lines + 2 lead files): **`51.86s`**
- Report generation (5 files): **`16.07s`**
- **Total end-to-end execution:** **`107.93s`** (under the 2-minute performance stretch target)
- `lead_rows_total`: 143,990 (`7,097` rejected, `3,990` merged duplicates -> `132,903` unique leads)
- `events_total`: 632,602 (`631` malformed, `18,490` duplicate dropped, `34,424` orphan, `1,005` unknown disposition)

## If I had another day
1. **PostgreSQL Migration with Monthly Partitioning:** Use `docker-compose.yml` to run Postgres, partitioning `interaction_ledger` by `occurred_at_utc` range.
2. **KMS Key Management:** Store and rotate master encryption keys using AWS KMS or GCP Cloud KMS with key-version tagging on encrypted payloads.
3. **Dead-Letter Queue:** Automatically quarantine and notify on unmapped CSV columns or malformed JSON payloads.
