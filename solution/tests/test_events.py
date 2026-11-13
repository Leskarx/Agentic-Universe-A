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

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)


def _write_jsonl(path: Path, lines: list[str | dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            if isinstance(line, dict):
                f.write(json.dumps(line) + "\n")
            else:
                f.write(line + "\n")


class TestEventsIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "events_test.db"
        self.schemas = load_schemas(SCHEMAS_DIR)
        self.dispositions = load_dispositions(SCHEMAS_DIR)
        self.crypto = PiiCrypto.from_db_path(self.db_path)
        self.conn = connect_db(self.db_path)
        init_db(self.conn, self.dispositions)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_malformed_and_duplicate_events(self):
        drop = self.root / "drop_1"
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [
                "not-valid-json",
                {"missing_event_id": True},
                {"event_id": "evt001", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"},
                {"event_id": "evt001", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"},  # duplicate in same file
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop)

        stat = self.conn.execute("SELECT events_total, malformed_events, duplicate_events_dropped FROM ingested_files").fetchone()
        self.assertEqual(stat["events_total"], 4)
        self.assertEqual(stat["malformed_events"], 2)
        self.assertEqual(stat["duplicate_events_dropped"], 1)

        ledger_rows = self.conn.execute("SELECT * FROM interaction_ledger").fetchall()
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["event_id"], "evt001")

    def test_unresolved_events_quarantine_and_resolution(self):
        # Drop 1: Event arrives BEFORE lead exists
        drop1 = self.root / "drop_1"
        _write_jsonl(
            drop1 / "events" / "events_0001.jsonl",
            [
                # Override arrives before call initiation, and before lead exists
                {"event_id": "evt_ovr", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c100", "occurred_at": "2026-10-01T11:00:00Z", "payload": {"disposition_code": "renewed"}},
                {"event_id": "evt_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c100", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"},
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop1)

        # Both events in unresolved_events table because lead 9876543210 does not exist yet
        unresolved = self.conn.execute("SELECT event_id, unresolved_reason, resolved_at FROM unresolved_events").fetchall()
        self.assertEqual(len(unresolved), 2)
        for u in unresolved:
            self.assertIsNone(u["resolved_at"])

        # Drop 2: Lead arrives!
        drop2 = self.root / "drop_2"
        _write_csv(
            drop2 / "leads" / "motor_renewal" / "batch_001.csv",
            [
                ["customer_name", "phone", "policy_number"],
                ["Rahul", "9876543210", "MOT/100"],
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop2)

        # Now both pending events are resolved!
        unresolved_after = self.conn.execute("SELECT event_id, resolved_at FROM unresolved_events").fetchall()
        for u in unresolved_after:
            self.assertIsNotNone(u["resolved_at"])

    def test_conflicting_call_phone_mappings_detected(self):
        drop = self.root / "drop_1"
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [
                {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c_conflict", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"},
                {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c_conflict", "occurred_at": "2026-10-01T10:00:10Z", "phone": "8765432109"},  # different phone for same call!
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop)

        # Check call_phone_links has is_conflicted = 1
        link = self.conn.execute("SELECT is_conflicted FROM call_phone_links WHERE call_id = 'c_conflict'").fetchone()
        self.assertEqual(link["is_conflicted"], 1)

        # Check conflict table logged
        conflicts = self.conn.execute("SELECT * FROM call_phone_conflicts").fetchall()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["call_id"], "c_conflict")
        self.assertEqual(conflicts[0]["event_id"], "e2")

        # Check e2 is quarantined with unresolved_reason = conflicting_call_phone
        u = self.conn.execute("SELECT unresolved_reason, resolved_at FROM unresolved_events WHERE event_id = 'e2'").fetchone()
        self.assertEqual(u["unresolved_reason"], "conflicting_call_phone")
        self.assertIsNone(u["resolved_at"])


if __name__ == "__main__":
    unittest.main()
