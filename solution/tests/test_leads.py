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


class TestLeadsIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "leads_test.db"
        self.schemas = load_schemas(SCHEMAS_DIR)
        self.dispositions = load_dispositions(SCHEMAS_DIR)
        self.crypto = PiiCrypto.from_db_path(self.db_path)
        self.conn = connect_db(self.db_path)
        init_db(self.conn, self.dispositions)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_order_rejections_blank_rows_and_warnings(self):
        drop = self.root / "drop_1"
        _write_csv(
            drop / "leads" / "motor_renewal" / "batch_001.csv",
            [
                ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language", "unknown_col"],
                ["", "", "", "", "", "", ""],  # Blank row -> skipped & NOT counted in row numbers
                ["", "9876543210", "MOT/1", "2026-10-10", "1000", "en", "extra"],  # row 1: missing_required:customer_name
                ["Rahul", "12345", "MOT/2", "2026-10-10", "1000", "en", "extra"],  # row 2: invalid_phone
                ["Rahul", "9876543210", "", "2026-10-10", "1000", "en", "extra"],  # row 3: missing_required:policy_number
                ["Alice", "9876543211", "MOT/4", "31/02/2026", "-500", "French", "ignored"],  # row 4: valid, 3 field warnings
            ],
        )

        ingest_path(self.conn, self.crypto, self.schemas, drop)

        # Check rejections
        rejs = self.conn.execute("SELECT row_number, reason FROM rejections ORDER BY row_number").fetchall()
        self.assertEqual(len(rejs), 3)
        self.assertEqual(rejs[0]["row_number"], 1)
        self.assertEqual(rejs[0]["reason"], "missing_required:customer_name")
        self.assertEqual(rejs[1]["row_number"], 2)
        self.assertEqual(rejs[1]["reason"], "invalid_phone")
        self.assertEqual(rejs[2]["row_number"], 3)
        self.assertEqual(rejs[2]["reason"], "missing_required:policy_number")

        # Check valid lead inserted
        lead = self.conn.execute("SELECT * FROM leads").fetchone()
        self.assertIsNotNone(lead)
        self.assertEqual(lead["external_ref"], "MOT/4")
        self.assertEqual(lead["batch_id"], "batch_001")
        self.assertEqual(lead["duplicate_count"], 1)

        # Check warnings recorded in ingested_files
        stat = self.conn.execute("SELECT rows_total, rows_rejected, field_warnings FROM ingested_files").fetchone()
        self.assertEqual(stat["rows_total"], 4)
        self.assertEqual(stat["rows_rejected"], 3)
        self.assertEqual(stat["field_warnings"], 3)  # only row 4 warnings counted

    def test_duplicate_merging_and_counter(self):
        drop1 = self.root / "drop_1"
        _write_csv(
            drop1 / "leads" / "motor_renewal" / "batch_001.csv",
            [
                ["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"],
                ["Rahul", "9876543210", "MOT/001", "15000", "hi"],
            ],
        )
        drop2 = self.root / "drop_2"
        _write_csv(
            drop2 / "leads" / "motor_renewal" / "batch_002.csv",
            [
                ["Insured Name", "Mobile No", "Policy No", "Premium", "Language"],
                ["Rahul Sharma", "+91 98765 43210", "MOT/001", "16000", ""],  # Language empty -> keep 'hi'
            ],
        )
        drop3 = self.root / "drop_3"
        _write_csv(
            drop3 / "leads" / "motor_renewal" / "batch_003.csv",
            [
                ["Insured Name", "Mobile No", "Policy No", "Premium", "Language"],
                ["Rahul S.", "09876543210", "MOT/001", "", "ta"],  # Premium empty -> keep 16000.00
            ],
        )

        ingest_path(self.conn, self.crypto, self.schemas, drop1)
        ingest_path(self.conn, self.crypto, self.schemas, drop2)
        ingest_path(self.conn, self.crypto, self.schemas, drop3)

        leads = self.conn.execute("SELECT * FROM leads").fetchall()
        self.assertEqual(len(leads), 1)
        lead = leads[0]

        # batch_id must remain batch_001 permanently
        self.assertEqual(lead["batch_id"], "batch_001")
        self.assertEqual(lead["duplicate_count"], 3)

        attrs = json.loads(lead["attributes_json"])
        self.assertEqual(attrs["premium_amount"], "16000.00")
        self.assertEqual(attrs["preferred_language"], "ta")

        pii = self.crypto.decrypt_json(lead["pii_attributes_enc"])
        self.assertEqual(pii["customer_name"], "Rahul S.")


if __name__ == "__main__":
    unittest.main()
