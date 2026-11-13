from __future__ import annotations

import unittest

from solution.au_pipeline.normalizers import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    parse_timestamp,
)


class TestNormalizers(unittest.TestCase):
    def test_normalize_header(self):
        self.assertEqual(normalize_header("Insured Name"), "insured name")
        self.assertEqual(normalize_header("  customer___name  "), "customer name")
        self.assertEqual(normalize_header("Mobile_No"), "mobile no")
        self.assertEqual(normalize_header("  __Policy   No__ "), "policy no")

    def test_normalize_phone_valid_formats(self):
        expected = "+919876543210"
        self.assertEqual(normalize_phone("9876543210"), expected)
        self.assertEqual(normalize_phone("+91 98765 43210"), expected)
        self.assertEqual(normalize_phone("09876543210"), expected)
        self.assertEqual(normalize_phone("91-98765-43210"), expected)
        self.assertEqual(normalize_phone("(+91) 9876543210"), expected)
        self.assertEqual(normalize_phone("+919876543210"), expected)
        # Leading digits 6, 7, 8, 9
        self.assertEqual(normalize_phone("6000000001"), "+916000000001")
        self.assertEqual(normalize_phone("7000000001"), "+917000000001")
        self.assertEqual(normalize_phone("8000000001"), "+918000000001")

    def test_normalize_phone_invalid(self):
        self.assertIsNone(normalize_phone("987654321"))          # 9 digits
        self.assertIsNone(normalize_phone("5876543210"))         # bad prefix 5
        self.assertIsNone(normalize_phone("022-2345-6789"))      # landline
        self.assertIsNone(normalize_phone("+1 415 555 0134"))    # US number
        self.assertIsNone(normalize_phone("N/A"))
        self.assertIsNone(normalize_phone(""))

    def test_normalize_date_formats_and_day_first(self):
        self.assertEqual(normalize_date("2026-12-01"), "2026-12-01")
        self.assertEqual(normalize_date("01/12/2026"), "2026-12-01")  # day-first!
        self.assertEqual(normalize_date("01-12-2026"), "2026-12-01")
        self.assertEqual(normalize_date("01-Dec-2026"), "2026-12-01")
        self.assertEqual(normalize_date("01-dec-2026"), "2026-12-01")
        self.assertEqual(normalize_date("29/02/2024"), "2024-02-29")  # leap year

    def test_normalize_date_invalid(self):
        self.assertIsNone(normalize_date("31/02/2026"))
        self.assertIsNone(normalize_date("29/02/2025"))  # non-leap year
        self.assertIsNone(normalize_date("2026-13-01"))
        self.assertIsNone(normalize_date("soon"))
        self.assertIsNone(normalize_date(""))

    def test_normalize_decimal(self):
        self.assertEqual(normalize_decimal("6850"), "6850.00")
        self.assertEqual(normalize_decimal("29678.29"), "29678.29")
        self.assertEqual(normalize_decimal("₹27,554"), "27554.00")
        self.assertEqual(normalize_decimal("Rs. 31,629.5"), "31629.50")
        self.assertEqual(normalize_decimal("rs 100"), "100.00")
        self.assertEqual(normalize_decimal("INR 25301.4"), "25301.40")
        self.assertEqual(normalize_decimal("inr10.005"), "10.01")
        self.assertEqual(normalize_decimal("0"), "0.00")
        self.assertIsNone(normalize_decimal("-50"))
        self.assertIsNone(normalize_decimal("N/A"))
        self.assertIsNone(normalize_decimal("Rs."))
        self.assertIsNone(normalize_decimal("NaN"))

    def test_normalize_enum(self):
        allowed = ("en", "hi", "ta", "kn", "te", "mr")
        self.assertEqual(normalize_enum("EN", allowed), "en")
        self.assertEqual(normalize_enum(" ta ", allowed), "ta")
        self.assertIsNone(normalize_enum("Hindi", allowed))
        self.assertIsNone(normalize_enum("", allowed))

    def test_parse_timestamp_ist_conversion(self):
        # 20:00 UTC == 01:30 next day IST
        utc, ist, ist_date = parse_timestamp("2026-10-01T20:00:00Z")
        self.assertEqual(ist, "2026-10-02T01:30:00+05:30")
        self.assertEqual(ist_date, "2026-10-02")
        self.assertEqual(utc, "2026-10-01T20:00:00.000000Z")

        # Offset +05:30
        _, ist2, date2 = parse_timestamp("2026-10-05T18:50:21+05:30")
        self.assertEqual(ist2, "2026-10-05T18:50:21+05:30")
        self.assertEqual(date2, "2026-10-05")

        # Naive means IST
        utc3, ist3, date3 = parse_timestamp("2026-10-03 10:01:00")
        self.assertEqual(ist3, "2026-10-03T10:01:00+05:30")
        self.assertEqual(date3, "2026-10-03")
        self.assertEqual(utc3, "2026-10-03T04:31:00.000000Z")

        self.assertIsNone(parse_timestamp("not-a-timestamp"))


if __name__ == "__main__":
    unittest.main()
