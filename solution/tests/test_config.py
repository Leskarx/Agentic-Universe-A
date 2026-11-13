from __future__ import annotations

import unittest
from pathlib import Path

from solution.au_pipeline.config import load_dispositions, load_schemas, resolve_attributes_with_defaults

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


class TestConfig(unittest.TestCase):
    def test_load_schemas(self):
        schemas = load_schemas(SCHEMAS_DIR)
        self.assertIn("motor_renewal", schemas)
        self.assertIn("policy_feedback", schemas)

        motor = schemas["motor_renewal"]
        self.assertEqual(motor.reference_field, "policy_number")
        self.assertIn("insured name", motor.header_map)
        self.assertEqual(motor.header_map["insured name"], "customer_name")
        self.assertEqual(motor.defaults.get("preferred_language"), "en")

        feedback = schemas["policy_feedback"]
        self.assertEqual(feedback.reference_field, "order_id")
        self.assertIn("contact", feedback.header_map)
        self.assertEqual(feedback.header_map["contact"], "phone")

    def test_load_dispositions(self):
        disps = load_dispositions(SCHEMAS_DIR)
        self.assertIn(("motor_renewal", "renewed"), disps)
        self.assertTrue(disps[("motor_renewal", "renewed")].is_terminal)
        self.assertTrue(disps[("motor_renewal", "renewed")].is_success)

        self.assertIn(("motor_renewal", "callback_requested"), disps)
        self.assertFalse(disps[("motor_renewal", "callback_requested")].is_terminal)
        self.assertFalse(disps[("motor_renewal", "callback_requested")].is_success)

        # Check policy_feedback refused
        self.assertIn(("policy_feedback", "refused"), disps)
        refused = disps[("policy_feedback", "refused")]
        self.assertTrue(refused.is_terminal)
        self.assertFalse(refused.is_success)

    def test_resolve_attributes_with_defaults(self):
        schemas = load_schemas(SCHEMAS_DIR)
        motor = schemas["motor_renewal"]

        # Missing language gets default 'en'
        attrs = resolve_attributes_with_defaults(motor, '{"premium_amount":"15000"}')
        self.assertEqual(attrs["preferred_language"], "en")
        self.assertEqual(attrs["premium_amount"], "15000")

        # Existing language is preserved
        attrs2 = resolve_attributes_with_defaults(motor, '{"preferred_language":"hi"}')
        self.assertEqual(attrs2["preferred_language"], "hi")


if __name__ == "__main__":
    unittest.main()
