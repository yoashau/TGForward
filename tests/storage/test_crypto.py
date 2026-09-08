import base64

import pytest
from cryptography.exceptions import InvalidTag

from tgforward.storage.crypto import decrypt, encrypt

# 用 PBKDF2/AES 方案（PBKDF2-SHA256 / 100000 轮 / AES-128-GCM）预生成的密文，
# 用于验证存量数据库凭证的解密兼容性。
LEGACY_CIPHERTEXT = "AAAAAAAAAAAAAAAAo8y7YW6Hx9lMNdhOlBnbJXmhvD25LPqSTP7SsfgUBfYb8KcX1Mom9bfCSA=="
LEGACY_PLAINTEXT = "BQAbc123LegacySessionString"


class TestRoundtrip:
    def test_roundtrip(self):
        secret = "session-string-abc-123"
        assert decrypt(encrypt(secret)) == secret

    def test_unicode(self):
        secret = "会话凭证🔐"
        assert decrypt(encrypt(secret)) == secret

    def test_unique_nonce(self):
        assert encrypt("x") != encrypt("x")


class TestLegacyCompat:
    def test_decrypt_legacy_ciphertext(self, monkeypatch):
        from tgforward.config import _PUBLIC_MASTER_KEY, _PUBLIC_SALT
        from tgforward.storage import crypto

        # 仅验证旧密文格式；显式选择该 fixture 的密钥，不再依赖生产默认值。
        monkeypatch.setattr(crypto, "_KEY", crypto._derive_key(_PUBLIC_MASTER_KEY, _PUBLIC_SALT))
        assert decrypt(LEGACY_CIPHERTEXT) == LEGACY_PLAINTEXT


class TestTamper:
    def test_tampered_ciphertext_rejected(self):
        token = encrypt("secret")
        raw = bytearray(base64.b64decode(token.removeprefix("v1:")))
        raw[-1] ^= 0xFF
        with pytest.raises(InvalidTag):
            decrypt("v1:" + base64.b64encode(bytes(raw)).decode())
