from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class PiiCrypto:
    """Handles keyed HMAC-SHA256 lookups and AES-256-GCM encryption for PII at rest."""

    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError("Master key must be exactly 32 bytes")
        self._enc_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"au-interaction-ledger-v1",
            info=b"pii-aes-256-gcm",
        ).derive(master_key)
        self._hmac_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"au-interaction-ledger-v1",
            info=b"phone-hmac-sha256",
        ).derive(master_key)
        self._aesgcm = AESGCM(self._enc_key)

    @classmethod
    def from_db_path(cls, db_path: str | Path, key_bytes: bytes | None = None) -> PiiCrypto:
        if key_bytes is not None:
            return cls(key_bytes)

        env_key = os.environ.get("AU_PII_KEY", "").strip()
        if env_key:
            return cls(_decode_key(env_key))

        if str(db_path) == ":memory:":
            return cls(os.urandom(32))

        db_file = Path(db_path)
        key_file = Path(f"{db_file}.key")
        if key_file.exists():
            raw = key_file.read_text(encoding="utf-8").strip()
            return cls(_decode_key(raw))

        if db_file.exists() and db_file.stat().st_size > 0:
            raise RuntimeError(
                f"Database '{db_file}' exists but PII key file '{key_file}' and AU_PII_KEY are missing."
            )

        db_file.parent.mkdir(parents=True, exist_ok=True)
        master_key = os.urandom(32)
        encoded = base64.urlsafe_b64encode(master_key).decode("ascii")
        fd = os.open(key_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (encoded + "\n").encode("ascii"))
        finally:
            os.close(fd)
        return cls(master_key)

    def phone_hmac(self, canonical_phone: str) -> bytes:
        return hmac.new(self._hmac_key, canonical_phone.encode("utf-8"), hashlib.sha256).digest()

    def encrypt_str(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return nonce + ct

    def decrypt_str(self, blob: bytes) -> str:
        nonce, ct = blob[:12], blob[12:]
        return self._aesgcm.decrypt(nonce, ct, None).decode("utf-8")

    def encrypt_json(self, data: dict[str, Any]) -> bytes:
        serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return self.encrypt_str(serialized)

    def decrypt_json(self, blob: bytes) -> dict[str, Any]:
        raw = self.decrypt_str(blob)
        return json.loads(raw) if raw else {}


def _decode_key(raw: str) -> bytes:
    if len(raw) == 64:
        try:
            b = bytes.fromhex(raw)
            if len(b) == 32:
                return b
        except ValueError:
            pass
    b = base64.urlsafe_b64decode(raw.encode("ascii"))
    if len(b) != 32:
        raise ValueError("AU_PII_KEY must decode to 32 bytes (hex or URL-safe base64)")
    return b
