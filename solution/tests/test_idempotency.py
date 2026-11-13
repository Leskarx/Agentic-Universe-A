from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from solution.au_pipeline.config import load_dispositions, load_schemas
from solution.au_pipeline.crypto import PiiCrypto
from solution.au_pipeline.db import connect_db, init_db
from solution.au_pipeline.ingest import ingest_path
from solution.au_pipeline.report import generate_reports

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


class TestIdempotencyAndSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.schemas = load_schemas(SCHEMAS_DIR)
        self.dispositions = load_dispositions(SCHEMAS_DIR)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reingest_idempotency(self):
        drop1 = self.root / "drop_1"
        _write_csv(
            drop1 / "leads" / "motor_renewal" / "batch_001.csv",
            [["customer_name", "phone", "policy_number"], ["User One", "9111111111", "MOT/1"]],
        )
        _write_jsonl(
            drop1 / "events" / "events_0001.jsonl",
            [
                {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00+05:30", "phone": "9111111111"},
            ],
        )

        db_path = self.root / "idemp.db"
        crypto = PiiCrypto.from_db_path(db_path)
        conn = connect_db(db_path)
        init_db(conn, self.dispositions)

        # First run: ingested
        s1 = ingest_path(conn, crypto, self.schemas, drop1)
        self.assertEqual(s1["lead_files_ingested"], 1)
        self.assertEqual(s1["event_files_ingested"], 1)

        # Second run: completely skipped, zero changes
        s2 = ingest_path(conn, crypto, self.schemas, drop1)
        self.assertEqual(s2["lead_files_ingested"], 0)
        self.assertEqual(s2["lead_files_skipped"], 1)
        self.assertEqual(s2["event_files_ingested"], 0)
        self.assertEqual(s2["event_files_skipped"], 1)

        # Verify ingested_files audit count didn't duplicate
        rows = conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0]
        self.assertEqual(rows, 2)
        conn.close()

    def test_append_only_triggers(self):
        db_path = self.root / "trigger.db"
        crypto = PiiCrypto.from_db_path(db_path)
        conn = connect_db(db_path)
        init_db(conn, self.dispositions)

        drop = self.root / "drop"
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [{"event_id": "e_perm", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"}],
        )
        ingest_path(conn, crypto, self.schemas, drop)

        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE interaction_ledger SET event_type = 'call.tampered' WHERE event_id = 'e_perm'")

        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM interaction_ledger WHERE event_id = 'e_perm'")

        conn.close()

    def test_zero_plaintext_pii_in_database(self):
        db_path = self.root / "pii.db"
        crypto = PiiCrypto.from_db_path(db_path)
        conn = connect_db(db_path)
        init_db(conn, self.dispositions)

        drop = self.root / "drop_pii"
        _write_csv(
            drop / "leads" / "motor_renewal" / "batch_001.csv",
            [["customer_name", "phone", "policy_number"], ["SuperSecretCustomerNameXYZ", "+91 99887 76655", "MOT/999"]],
        )
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [{"event_id": "e_pii", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c99", "occurred_at": "2026-10-01T10:00:00Z", "phone": "+91 99887 76655"}],
        )
        ingest_path(conn, crypto, self.schemas, drop)
        conn.close()

        # Check raw database file bytes
        raw_bytes = db_path.read_bytes()
        wal_path = Path(f"{db_path}-wal")
        if wal_path.exists():
            raw_bytes += wal_path.read_bytes()

        self.assertNotIn(b"SuperSecretCustomerNameXYZ", raw_bytes)
        self.assertNotIn(b"9988776655", raw_bytes)
        self.assertNotIn(b"99887", raw_bytes)


if __name__ == "__main__":
    unittest.main()
