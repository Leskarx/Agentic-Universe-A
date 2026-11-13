from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .config import load_dispositions, load_schemas
from .crypto import PiiCrypto
from .db import connect_db, init_db
from .ingest import ingest_path
from .report import generate_reports

DEFAULT_DB = Path(os.environ.get("AU_DB_PATH", "au_ledger.db"))
DEFAULT_SCHEMAS = Path(os.environ.get("AU_SCHEMAS_DIR", "assignment-a-data/schemas"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="au-pipeline",
        description="Agentic Universe Interaction Ledger & Reporting Pipeline",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="Path to SQLite database file (default: AU_DB_PATH or ./au_ledger.db)",
    )
    parser.add_argument(
        "--schemas-dir",
        type=Path,
        default=DEFAULT_SCHEMAS,
        help="Path to workflow schemas and dispositions.csv (default: assignment-a-data/schemas)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest a drop directory (or directory of drops)")
    p_ingest.add_argument("path", type=Path, help="Drop directory path, e.g. data/drop_1")

    p_report = sub.add_parser("report", help="Generate the 5 report files from the ledger")
    p_report.add_argument("--out", type=Path, required=True, help="Output directory for reports")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    schemas = load_schemas(args.schemas_dir)
    dispositions = load_dispositions(args.schemas_dir)
    crypto = PiiCrypto.from_db_path(args.db)
    conn = connect_db(args.db)
    try:
        init_db(conn, dispositions)
        if args.command == "ingest":
            summary = ingest_path(conn, crypto, schemas, args.path)
            dt = time.perf_counter() - t0
            print(
                f"Ingested {summary['drops_processed']} drop(s) in {dt:.2f}s: "
                f"{summary['lead_files_ingested']} lead file(s) ({summary['lead_files_skipped']} skipped), "
                f"{summary['event_files_ingested']} event file(s) ({summary['event_files_skipped']} skipped)."
            )
        elif args.command == "report":
            paths = generate_reports(conn, crypto, args.out)
            dt = time.perf_counter() - t0
            print(f"Generated {len(paths)} report files in {args.out} in {dt:.2f}s.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
