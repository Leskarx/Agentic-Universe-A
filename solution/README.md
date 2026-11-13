# Assignment A: Interaction Ledger & Reporting Pipeline

This directory contains the Assignment A solution. The command-line application ingests lead CSVs and call-event JSONL files into SQLite, then builds the five contract reports from the stored data.

## Requirements

- Python 3.11 or newer
- Dependencies from the repository's `requirements.txt`

## Setup and run

Run these commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Generate the sample input files (generated data is ignored by Git):

```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

Ingest drops in order, then generate reports:

```bash
python solution/cli.py ingest data/drop_1
python solution/cli.py ingest data/drop_2
python solution/cli.py report --out reports/
```

To ingest all discovered drops under one parent directory, use `python solution/cli.py ingest data`. The same database accumulates state across commands, so a report after each ingest includes all successfully ingested drops to date. For example, ingest `drop_1`, report, ingest `drop_2`, then report again to see the updated state.

The report command writes these files to the directory supplied with `--out`:

- `batch_summary.csv`
- `daily_overview.csv`
- `lead_report.csv`
- `rejections.csv`
- `data_quality.json`

Run the automated test suite with:

```bash
python -m unittest discover -s solution/tests -v
```

## Database and file locations

By default, the program creates `au_ledger.db` in the current working directory. `ingest` and `report` use the same database by default. Override it with the top-level `--db` option, which must appear before the subcommand:

```bash
python solution/cli.py --db /path/to/ledger.db ingest data/drop_1
python solution/cli.py --db /path/to/ledger.db report --out reports/
```

Alternatively, set `AU_DB_PATH` to choose a default database path. The `--db` option takes precedence. The schema and disposition catalogue directory defaults to `assignment-a-data/schemas`; override it with `--schemas-dir` or `AU_SCHEMAS_DIR`.

The database contains:

- `leads`: one row per `(workflow_id, phone)` identity, with the original batch ID, merged attributes, and a duplicate count.
- `interaction_ledger`: the append-only event history. Database triggers reject updates and deletes.
- `call_phone_links` and `call_phone_conflicts`: event-to-lead call mapping and conflict audit records.
- `unresolved_events`: events that cannot yet be linked. These can resolve when a later drop supplies the required lead or call link.
- `rejections` and `ingested_files`: rejected lead row details and file-level processing counts used by reports.
- `dispositions`: the loaded disposition catalogue used by report calculations.

Each input file is tracked by its relative path and SHA-256 content hash. Re-ingesting a file with the same path and contents skips it. A changed file is treated as a new file version; rows from the previous version are not rolled back automatically. Different paths are treated as different inputs, even if their contents match.

## PII protection

Phone numbers are normalized before matching. The database stores a keyed HMAC of each canonical phone for indexed identity matching and an AES-GCM encrypted phone for masked report output. Other schema fields marked `pii: true` are stored together in an encrypted JSON value. Event payloads have phone and customer-name keys removed before storage.

The encryption key is stored separately at `<database path>.key` (for example, `au_ledger.db.key`) unless `AU_PII_KEY` is supplied. Keep the key safe and available with the database: losing it prevents decrypting stored PII and generating masked phone values. Do not commit the database, key, generated input data, or report output.

## Architecture and code map

```text
CSV lead files ─┐                         ┌─> batch_summary.csv
                ├─> validation/ingestion ├─> daily_overview.csv
JSONL events ───┘          │              ├─> lead_report.csv
                           ▼              ├─> rejections.csv
                    SQLite database ──────└─> data_quality.json
```

- `solution/cli.py`: repository-root command entry point.
- `solution/au_pipeline/__main__.py`: CLI arguments, configuration loading, database setup, and command dispatch.
- `solution/au_pipeline/config.py`: workflow schema and disposition loading.
- `solution/au_pipeline/normalizers.py`: header, phone, date, decimal, enum, and timestamp normalization.
- `solution/au_pipeline/crypto.py`: key derivation, phone HMAC, and AES-GCM encryption.
- `solution/au_pipeline/db.py`: SQLite schema, indexes, and append-only ledger triggers.
- `solution/au_pipeline/ingest.py`: ordered, transactional, idempotent file ingestion and event linking.
- `solution/au_pipeline/report.py`: SQL-based report calculations and CSV/JSON output.
- `solution/tests/`: tests for configuration, normalization, encryption, ingestion, dispositions, reports, and idempotency.
