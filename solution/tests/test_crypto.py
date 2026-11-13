from __future__ import annotations

import os
import tempfile
import unittest

from solution.au_pipeline.crypto import PiiCrypto


class TestCrypto(unittest.TestCase):
    def setUp(self):
        self.key = os.urandom(32)
        self.crypto = PiiCrypto(self.key)

    def test_key_separation_and_hmac_consistency(self):
        phone = "+919876543210"
        h1 = self.crypto.phone_hmac(phone)
        h2 = self.crypto.phone_hmac(phone)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 32)

        # Different key produces different HMAC
        crypto2 = PiiCrypto(os.urandom(32))
        self.assertNotEqual(h1, crypto2.phone_hmac(phone))

    def test_aes_gcm_encrypt_decrypt_str(self):
        text = "Rahul Sharma"
        ciphertext = self.crypto.encrypt_str(text)
        self.assertNotEqual(ciphertext, text.encode("utf-8"))
        self.assertNotIn(b"Rahul", ciphertext)

        decrypted = self.crypto.decrypt_str(ciphertext)
        self.assertEqual(decrypted, text)

        # Different encryptions of same text have different random nonces
        ct2 = self.crypto.encrypt_str(text)
        self.assertNotEqual(ciphertext, ct2)
        self.assertEqual(self.crypto.decrypt_str(ct2), text)

    def test_aes_gcm_encrypt_decrypt_json(self):
        data = {"customer_name": "Aarav Menon", "email": "aarav@example.com"}
        ciphertext = self.crypto.encrypt_json(data)
        self.assertNotIn(b"Aarav", ciphertext)

        recovered = self.crypto.decrypt_json(ciphertext)
        self.assertEqual(recovered, data)

    def test_wrong_key_fails(self):
        ciphertext = self.crypto.encrypt_str("Secret")
        crypto_wrong = PiiCrypto(os.urandom(32))
        with self.assertRaises(Exception):
            crypto_wrong.decrypt_str(ciphertext)

    def test_key_file_creation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test.db")
            c = PiiCrypto.from_db_path(db_path)
            key_file = db_path + ".key"
            self.assertTrue(os.path.exists(key_file))

            # Re-loading from same db_path loads existing key
            c2 = PiiCrypto.from_db_path(db_path)
            self.assertEqual(c._enc_key, c2._enc_key)
            self.assertEqual(c._hmac_key, c2._hmac_key)


if __name__ == "__main__":
    unittest.main()
