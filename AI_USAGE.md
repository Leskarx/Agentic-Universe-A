# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
Antigravity (Gemini 3.8 Flash)

## What I used them for
- Surveying the repository, schemas, and `generate_data.py` edge cases
- Designing and implementing the modular pipeline package under `solution/au_pipeline/` and CLI (`solution/cli.py`)
- Writing SQLite DDL, triggers for append-only guarantees, `unresolved_events` quarantine tables, and SQL CTE/window-function reporting queries
- Writing the comprehensive 8-module test suite in `solution/tests/` (28 unit and integration tests)
- Benchmarking the large dataset and verifying incremental equivalence and PII-at-rest guarantees

## What I wrote or decided myself
- Chose SQLite with WAL mode + append-only triggers (`BEFORE UPDATE` / `BEFORE DELETE`) over Python-side aggregation or external daemon dependencies
- Chose HKDF-SHA256 key separation with keyed HMAC-SHA256 for phone matching (avoiding unsalted SHA-256 over the small 10-digit Indian mobile space) and AES-256-GCM for encrypted PII columns
- Implemented a dedicated `unresolved_events` quarantine table to hold events arriving before leads or unlinked overrides, resolving them automatically when leads arrive
- Implemented schema-driven PII encryption (`pii_attributes_enc` holding an AES-256-GCM encrypted JSON payload of all fields with `pii: true`) so new workflows require zero schema migrations
- Designed conflicting call-to-phone detection in `call_phone_links` and `call_phone_conflicts` to prevent associating calls with the wrong lead
- Filtered unknown disposition codes before `ROW_NUMBER()` ranking so invalid overrides never shadow valid dispositions
- Maintained `duplicate_count` on leads and applied defaults (`preferred_language -> en`) dynamically on read rather than burning defaults into stored JSON

## Where the AI was wrong or unhelpful
- Initial module split introduced a circular import between `config.py` (`IST`) and `normalizers.py` (`normalize_header`), caught immediately on the first CLI run and resolved by moving `normalize_header` and `IST` into `normalizers.py`.
- The initial PII-at-rest test called `PRAGMA wal_checkpoint(TRUNCATE)` while the test connection was still open after trigger error assertions, raising `sqlite3.OperationalError: database table is locked`; fixed by closing the connection before reading `.db` and `.db-wal` bytes.
- An initial test assertion in `test_reports.py` miscounted the number of test event lines as 5 instead of 6; caught and corrected by reading the test fixture directly.

## How I verified AI-generated code
- Ran all 28 automated tests across 8 test modules covering normalizers, schemas, crypto, lead merges, event deduplication, disposition priorities, reports, and idempotency
- Verified byte-for-byte report equivalence (`diff -ru`) across incremental (`drop_1` -> `drop_2`), re-ingested (`drop_1` twice), and all-at-once ingestion on `--seed 42 --scale small`
- Benchmarked and verified row/count invariants on `--seed 42 --scale large` (~144k lead rows, ~633k event lines)
