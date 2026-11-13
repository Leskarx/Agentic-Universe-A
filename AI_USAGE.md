# AI Usage Disclosure

AI tools were used during development and review. This disclosure describes the tools and their contributions; the candidate should confirm that it accurately reflects their work before submission.

## Tools used

- **Antigravity (Gemini 3.8 Flash):** used during the initial implementation, as described below.
- **OpenAI Codex (GPT-6):** used in a later review and documentation pass.

## How AI tools were used

### Antigravity (Gemini 3.8 Flash)

- Surveyed the repository, schemas, and edge cases in `assignment-a-data/tools/generate_data.py`.
- Helped design and implement the pipeline package under `solution/au_pipeline/` and the CLI in `solution/cli.py`.
- Helped write SQLite schema and triggers, unresolved-event quarantine handling, and SQL report queries.
- Helped write the eight test modules in `solution/tests/` (28 tests).
- Was used for the original large-data benchmark and checks of incremental equivalence and PII-at-rest handling; the reported measurements are in `SUBMISSION.md`.

### OpenAI Codex (GPT-6)

- Reviewed the Assignment A architecture and implementation against the specification without changing files during the initial review.
- Found a duplicate-event edge case: a repeated `event_id` with a conflicting phone could change call-link and conflict state before the database rejected the duplicate event.
- Implemented a fix in `solution/au_pipeline/ingest.py` so duplicate IDs are detected before call-link and quarantine side effects. The existing test suite was rerun, and a targeted check confirmed that the conflicting duplicate is counted but does not create a conflict.
- Rewrote `solution/README.md` with setup, operation, storage, idempotency, and architecture information.

## Candidate decisions and review

The candidate selected Assignment A and the overall design choices documented in `SUBMISSION.md`, including SQLite, append-only event history, keyed HMAC phone matching, AES-GCM encryption, unresolved-event tracking, and SQL-based reporting. AI assistance contributed implementation and review suggestions; the candidate is responsible for reviewing the final code and being able to explain and maintain it.

## Issues found and corrected

- An initial module split introduced a circular import between `config.py` (`IST`) and `normalizers.py` (`normalize_header`). It was caught during the first CLI run and resolved by moving those definitions into `normalizers.py`.
- The initial PII-at-rest test called `PRAGMA wal_checkpoint(TRUNCATE)` while its test connection remained open, causing `sqlite3.OperationalError: database table is locked`. The connection was closed before the database and WAL files were inspected.
- An initial assertion in `test_reports.py` expected five event lines instead of six. It was corrected after checking the fixture.
- The later Codex review found the duplicate-event side-effect issue described above; the ingestion code was updated and checked with the test suite and a targeted scenario.

## Verification

- The 28 automated tests across eight modules pass with `python -m unittest discover -s solution/tests -v`.
- During the original implementation, report equivalence was checked across incremental (`drop_1` then `drop_2`), repeat-ingestion, and all-at-once runs on `--seed 42 --scale small`; the results are recorded in `SUBMISSION.md`.
- The original large-data benchmark and its counts are recorded in `SUBMISSION.md`.
- During the later review, a targeted duplicate-ID scenario with a conflicting phone confirmed that the duplicate is counted and does not create a call-phone conflict.
