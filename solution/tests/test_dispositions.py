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


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class TestDispositions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "disp_test.db"
        self.schemas = load_schemas(SCHEMAS_DIR)
        self.dispositions = load_dispositions(SCHEMAS_DIR)
        self.crypto = PiiCrypto.from_db_path(self.db_path)
        self.conn = connect_db(self.db_path)
        init_db(self.conn, self.dispositions)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_override_takes_priority_regardless_of_timestamp(self):
        """
        disposition.set: 10:00 AM -> renewed
        disposition.override: 09:30 AM -> not_interested
        disposition.set: 10:30 AM -> callback_requested
        Rule: An override takes priority over any set, so effective disposition is not_interested!
        """
        drop = self.root / "drop_1"
        _write_csv(
            drop / "leads" / "motor_renewal" / "batch_001.csv",
            [["customer_name", "phone", "policy_number"], ["Lead A", "9000000001", "MOT/A"]],
        )
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [
                {"event_id": "e_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T09:00:00+05:30", "phone": "9000000001"},
                {"event_id": "e_s1", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T10:00:00+05:30", "phone": "9000000001", "payload": {"disposition_code": "renewed"}},
                {"event_id": "e_ovr", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T09:30:00+05:30", "payload": {"disposition_code": "not_interested"}},
                {"event_id": "e_s2", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T10:30:00+05:30", "phone": "9000000001", "payload": {"disposition_code": "callback_requested"}},
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop)
        out = self.root / "reports"
        generate_reports(self.conn, self.crypto, out)

        leads = _read_csv_dicts(out / "lead_report.csv")
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["current_disposition"], "not_interested")
        self.assertEqual(leads[0]["bucket"], "completed")
        self.assertEqual(leads[0]["ever_success"], "false")

    def test_unknown_override_code_does_not_replace_valid_set(self):
        """
        disposition.set: renewed (valid)
        disposition.override: UNKNOWN_CODE (invalid)
        Rule: Unknown disposition codes are filtered out before selecting effective disposition.
        Therefore, renewed must win!
        """
        drop = self.root / "drop_1"
        _write_csv(
            drop / "leads" / "motor_renewal" / "batch_001.csv",
            [["customer_name", "phone", "policy_number"], ["Lead B", "9000000002", "MOT/B"]],
        )
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [
                {"event_id": "e_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-01T09:00:00+05:30", "phone": "9000000002"},
                {"event_id": "e_s", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-01T10:00:00+05:30", "phone": "9000000002", "payload": {"disposition_code": "renewed"}},
                {"event_id": "e_bad_ovr", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-01T10:30:00+05:30", "payload": {"disposition_code": "INVALID_OVERRIDE_CODE"}},
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop)
        out = self.root / "reports"
        generate_reports(self.conn, self.crypto, out)

        leads = _read_csv_dicts(out / "lead_report.csv")
        self.assertEqual(leads[0]["current_disposition"], "renewed")
        self.assertEqual(leads[0]["ever_success"], "true")

        dq = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
        self.assertEqual(dq["unknown_disposition_events"], 1)

    def test_multiple_valid_overrides_latest_wins(self):
        drop = self.root / "drop_1"
        _write_csv(
            drop / "leads" / "motor_renewal" / "batch_001.csv",
            [["customer_name", "phone", "policy_number"], ["Lead C", "9000000003", "MOT/C"]],
        )
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [
                {"event_id": "e_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-01T09:00:00+05:30", "phone": "9000000003"},
                {"event_id": "e_ovr1", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-01T09:30:00+05:30", "payload": {"disposition_code": "not_interested"}},
                {"event_id": "e_ovr2", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-01T11:00:00+05:30", "payload": {"disposition_code": "renewed"}},
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop)
        out = self.root / "reports"
        generate_reports(self.conn, self.crypto, out)

        leads = _read_csv_dicts(out / "lead_report.csv")
        self.assertEqual(leads[0]["current_disposition"], "renewed")
        self.assertEqual(leads[0]["ever_success"], "true")


if __name__ == "__main__":
    unittest.main()
