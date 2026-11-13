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


class TestReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "report_test.db"
        self.schemas = load_schemas(SCHEMAS_DIR)
        self.dispositions = load_dispositions(SCHEMAS_DIR)
        self.crypto = PiiCrypto.from_db_path(self.db_path)
        self.conn = connect_db(self.db_path)
        init_db(self.conn, self.dispositions)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_five_reports_format_and_calculations(self):
        drop = self.root / "drop_1"
        _write_csv(
            drop / "leads" / "policy_feedback" / "batch_001.csv",
            [
                ["customer_name", "phone", "order_id", "product"],
                ["Customer 1", "9800000001", "ORD-1", "Laptop"],   # connected & success
                ["Customer 2", "9800000002", "ORD-2", "Phone"],    # dialed, unanswered
                ["Customer 3", "9800000003", "ORD-3", "Tablet"],   # telephony failed
                ["Customer 4", "9800000004", "ORD-4", "Earbuds"],  # yet to reach out
                ["Bad Customer", "12345", "ORD-5", "Earbuds"],      # rejected row
            ],
        )
        _write_jsonl(
            drop / "events" / "events_0001.jsonl",
            [
                # Call 1 for Customer 1 (Connected, Feedback captured -> terminal=true, success=true)
                {"event_id": "e1_init", "event_type": "call.initiated", "workflow_id": "policy_feedback", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00+05:30", "phone": "9800000001"},
                {"event_id": "e1_conn", "event_type": "call.connected", "workflow_id": "policy_feedback", "call_id": "c1", "occurred_at": "2026-10-01T10:00:05+05:30", "phone": "9800000001"},
                {"event_id": "e1_disp", "event_type": "disposition.set", "workflow_id": "policy_feedback", "call_id": "c1", "occurred_at": "2026-10-01T10:02:00+05:30", "phone": "9800000001", "payload": {"disposition_code": "feedback_captured"}},

                # Call 2 for Customer 2 (Unanswered)
                {"event_id": "e2_init", "event_type": "call.initiated", "workflow_id": "policy_feedback", "call_id": "c2", "occurred_at": "2026-10-01T11:00:00+05:30", "phone": "9800000002"},

                # Call 3 for Customer 3 (Telephony failed)
                {"event_id": "e3_init", "event_type": "call.initiated", "workflow_id": "policy_feedback", "call_id": "c3", "occurred_at": "2026-10-01T12:00:00+05:30", "phone": "9800000003"},
                {"event_id": "e3_fail", "event_type": "call.telephony_failed", "workflow_id": "policy_feedback", "call_id": "c3", "occurred_at": "2026-10-01T12:00:03+05:30", "phone": "9800000003", "payload": {"reason": "sip_503"}},
            ],
        )
        ingest_path(self.conn, self.crypto, self.schemas, drop)
        out = self.root / "reports"
        generate_reports(self.conn, self.crypto, out)

        # 1. batch_summary.csv
        batch_rows = _read_csv_dicts(out / "batch_summary.csv")
        self.assertEqual(len(batch_rows), 1)
        b = batch_rows[0]
        self.assertEqual(b["workflow_id"], "policy_feedback")
        self.assertEqual(b["batch_id"], "batch_001")
        self.assertEqual(b["total_leads"], "4")
        self.assertEqual(b["yet_to_reach_out"], "1")
        self.assertEqual(b["in_progress_telephony_failed"], "1")
        self.assertEqual(b["in_progress_dialed"], "1")
        self.assertEqual(b["completed"], "1")

        # 2. daily_overview.csv
        daily_rows = _read_csv_dicts(out / "daily_overview.csv")
        self.assertEqual(len(daily_rows), 1)
        d = daily_rows[0]
        self.assertEqual(d["date"], "2026-10-01")
        self.assertEqual(d["total_calls"], "3")
        self.assertEqual(d["connected_calls"], "1")
        # Connectivity rate must have exactly 4 decimal places
        self.assertEqual(d["connectivity_rate"], "0.3333")
        self.assertEqual(d["success_leads"], "1")

        # 3. lead_report.csv
        lead_rows = {r["external_ref"]: r for r in _read_csv_dicts(out / "lead_report.csv")}
        self.assertEqual(len(lead_rows), 4)

        l1 = lead_rows["ORD-1"]
        self.assertEqual(l1["phone_masked"], "+91******0001")
        self.assertEqual(l1["bucket"], "completed")
        self.assertEqual(l1["total_calls"], "1")
        self.assertEqual(l1["connected_calls"], "1")
        self.assertEqual(l1["current_disposition"], "feedback_captured")
        self.assertEqual(l1["ever_success"], "true")
        self.assertEqual(l1["last_call_at"], "2026-10-01T10:00:00+05:30")

        l2 = lead_rows["ORD-2"]
        self.assertEqual(l2["bucket"], "in_progress_dialed")
        self.assertEqual(l2["current_disposition"], "")
        self.assertEqual(l2["ever_success"], "false")

        l3 = lead_rows["ORD-3"]
        self.assertEqual(l3["bucket"], "in_progress_telephony_failed")

        l4 = lead_rows["ORD-4"]
        self.assertEqual(l4["bucket"], "yet_to_reach_out")
        self.assertEqual(l4["last_call_at"], "")

        # 4. rejections.csv
        rejs = _read_csv_dicts(out / "rejections.csv")
        self.assertEqual(len(rejs), 1)
        self.assertEqual(rejs[0]["reason"], "invalid_phone")
        self.assertEqual(rejs[0]["row_number"], "5")

        # 5. data_quality.json
        dq = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
        self.assertEqual(dq["lead_rows_total"], 5)
        self.assertEqual(dq["lead_rows_rejected"], 1)
        self.assertEqual(dq["events_total"], 6)
        self.assertEqual(dq["orphan_events"], 0)


if __name__ == "__main__":
    unittest.main()
