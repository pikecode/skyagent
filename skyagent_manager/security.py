"""Authenticated files and native operating-system credential storage."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"SKYAGENT-ENC\x01"
PORTABLE_MAGIC = b"SKYAGENT-PORTABLE\x01"
MAX_FILE_SIZE = 256 * 1024 * 1024


class SecurityError(ValueError):
    pass


class MissingDatabaseKeyError(SecurityError):
    """An existing encrypted database must never receive a replacement key."""


class KeyVault:
    """Select native backends explicitly; never fall back to a plaintext backend."""

    def __init__(self, directory: Path):
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring

            self.backend = Keyring()
        elif sys.platform.startswith("win"):
            from keyring.backends.Windows import WinVaultKeyring

            self.backend = WinVaultKeyring()
        else:
            raise SecurityError("当前加密版本支持 macOS 和 Windows 系统凭据库。")
        namespace = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
        self.service = f"SkyAgentManager/{namespace}"

    def get(self, account: str) -> str | None:
        try:
            return self.backend.get_password(self.service, account)
        except Exception:
            raise SecurityError(
                "无法访问系统凭据库，请检查当前系统账户权限。"
            ) from None

    def set(self, account: str, value: str) -> None:
        try:
            self.backend.set_password(self.service, account, value)
        except Exception:
            raise SecurityError("无法保存到系统凭据库。") from None

    def delete(self, account: str) -> None:
        try:
            self.backend.delete_password(self.service, account)
        except Exception:
            raise SecurityError("无法从系统凭据库删除旧凭据。") from None

    def database_key(self, *, encrypted_exists: bool) -> bytes:
        encoded = self.get("database-key")
        if encoded is None:
            if encrypted_exists:
                raise MissingDatabaseKeyError(
                    "找不到此数据库的加密密钥。原数据库不会被覆盖。恢复原系统凭据库，或在新空目录恢复事先创建的跨机口令备份；普通备份仍需原密钥。"
                )
            key = AESGCM.generate_key(bit_length=256)
            self.set("database-key", base64.b64encode(key).decode("ascii"))
            return key
        try:
            key = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise SecurityError("系统凭据库中的数据库密钥无效。") from None
        if len(key) != 32:
            raise SecurityError("系统凭据库中的数据库密钥长度无效。")
        return key


def encrypt(data: bytes, key: bytes) -> bytes:
    if len(key) != 32:
        raise SecurityError("加密密钥必须为 256 位。")
    if len(data) + len(MAGIC) + 28 > MAX_FILE_SIZE:
        raise SecurityError("快照超过当前支持的 256 MiB 限制。")
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(data: bytes, key: bytes) -> bytes:
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 28:
        raise SecurityError("文件不是受支持的加密数据库或备份。")
    start = len(MAGIC)
    try:
        return AESGCM(key).decrypt(data[start : start + 12], data[start + 12 :], MAGIC)
    except InvalidTag:
        raise SecurityError("无法解密文件：密钥不匹配或文件已损坏。") from None


def read_file(path: Path) -> bytes:
    if path.stat().st_size > MAX_FILE_SIZE:
        raise SecurityError("文件超过当前支持的 256 MiB 限制。")
    return path.read_bytes()


def _portable_key(password: str, salt: bytes) -> bytes:
    if not 12 <= len(password) <= 1024:
        raise SecurityError("备份口令长度须为 12–1024 个字符，请使用独立的长口令。")
    # Format v1 fixes cost parameters; untrusted files cannot increase KDF cost.
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(
        password.encode("utf-8")
    )


def encrypt_portable(snapshot: bytes, password: str) -> bytes:
    if len(snapshot) + len(PORTABLE_MAGIC) + 44 > MAX_FILE_SIZE:
        raise SecurityError("快照超过当前支持的 256 MiB 限制。")
    salt = os.urandom(16)
    header = PORTABLE_MAGIC + salt
    nonce = os.urandom(12)
    key = _portable_key(password, salt)
    return header + nonce + AESGCM(key).encrypt(nonce, snapshot, header)


def decrypt_portable(data: bytes, password: str) -> bytes:
    if len(data) > MAX_FILE_SIZE:
        raise SecurityError("文件超过当前支持的 256 MiB 限制。")
    if not data.startswith(PORTABLE_MAGIC) or len(data) < len(PORTABLE_MAGIC) + 44:
        raise SecurityError("文件不是受支持的跨机加密备份。")
    end = len(PORTABLE_MAGIC) + 16
    key = _portable_key(password, data[len(PORTABLE_MAGIC) : end])
    try:
        return AESGCM(key).decrypt(data[end : end + 12], data[end + 12 :], data[:end])
    except InvalidTag:
        raise SecurityError("无法解密跨机备份：口令错误或文件已损坏。") from None


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
