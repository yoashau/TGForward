"""AES-GCM 凭证：新写 v1:base64(nonce+tag+ciphertext)，旧无前缀格式可读。

v1 前缀同时作为 AAD 认证；不同密钥迁移必须显式运行 tgforward.tools.rotate_keys。
"""

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from tgforward.config import MASTER_KEY, SALT_KEY

_NONCE_SIZE = 12
_TAG_SIZE = 16
_KEY_SIZE = 16  # 与旧实现保持一致，确保存量密文可解


def _derive_key(password: str = MASTER_KEY, salt: str = SALT_KEY) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=salt.encode(),
        iterations=100_000,
    )
    return kdf.derive(password.encode())


_KEY = _derive_key()
_PREFIX = "v1:"


def encrypt(text: str, *, key: bytes | None = None) -> str:
    nonce = os.urandom(_NONCE_SIZE)
    encryptor = Cipher(algorithms.AES(_KEY if key is None else key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_PREFIX.encode())
    ciphertext = encryptor.update(text.encode()) + encryptor.finalize()
    return _PREFIX + base64.b64encode(nonce + encryptor.tag + ciphertext).decode()


def decrypt(token: str, *, key: bytes | None = None) -> str:
    versioned = token.startswith(_PREFIX)
    if ":" in token and not versioned:
        raise ValueError("未知密文版本")
    data = base64.b64decode(token[len(_PREFIX) :] if versioned else token, validate=True)
    if len(data) < _NONCE_SIZE + _TAG_SIZE:
        raise ValueError("密文长度不足")
    nonce, tag, ciphertext = (
        data[:_NONCE_SIZE],
        data[_NONCE_SIZE : _NONCE_SIZE + _TAG_SIZE],
        data[_NONCE_SIZE + _TAG_SIZE :],
    )
    decryptor = Cipher(
        algorithms.AES(_KEY if key is None else key), modes.GCM(nonce, tag)
    ).decryptor()
    if versioned:
        decryptor.authenticate_additional_data(_PREFIX.encode())
    return (decryptor.update(ciphertext) + decryptor.finalize()).decode()
