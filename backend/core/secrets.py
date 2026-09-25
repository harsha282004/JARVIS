"""Secret files encrypted at rest with Windows DPAPI (per-user key), with a plaintext fallback where DPAPI does not exist.

DPAPI ties the encrypted blob to the Windows account: another user, or a copy of the file taken to another machine, cannot decrypt it, and no key
is ever stored by JARVIS. Files written here start with a marker; a file without the marker is read as plaintext (so tokens written before this
existed keep working and are re-written encrypted the next time they are saved). Secrets never reach logs (only exception types are logged).
"""

import base64
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from backend.core.logging import get_logger

logger = get_logger(__name__)

MARKER = "JARVIS-DPAPI1:"
_ENTROPY = b"jarvis-secret-store-v1"


class SecretError(Exception):
    """A secret could not be encrypted/decrypted (for example a file encrypted by a different Windows user)."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    src, keep1 = _blob(data)
    ent, keep2 = _blob(_ENTROPY)
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)) if protect else \
           (ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out))
    if not fn(*args):
        raise SecretError("DPAPI operation failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
        _ = keep1, keep2


def protect(data: bytes) -> bytes:
    return _crypt(data, True)


def unprotect(data: bytes) -> bytes:
    return _crypt(data, False)


def write_secret(path: Path, text: str, *, encrypt: bool = True) -> bool:
    """Write `text` to `path` (owner-only where the OS supports it). Returns True if it was encrypted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = False
    payload = text
    if encrypt and available():
        try:
            payload = MARKER + base64.b64encode(protect(text.encode("utf-8"))).decode("ascii")
            encrypted = True
        except Exception as exc:  # noqa: BLE001 - fall back rather than lose the secret; the caller learns it was not encrypted
            logger.warning("Secret could not be encrypted (%s); stored as plaintext in the user profile", type(exc).__name__)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp, path)
    return encrypted


def read_secret(path: Path) -> str:
    """The secret text. Raises FileNotFoundError if absent and SecretError if it cannot be decrypted."""
    raw = Path(path).read_text(encoding="utf-8")
    if not raw.startswith(MARKER):
        return raw
    if not available():
        raise SecretError("this secret was encrypted on Windows and cannot be read here")
    try:
        return unprotect(base64.b64decode(raw[len(MARKER):])).decode("utf-8")
    except Exception:  # noqa: BLE001
        raise SecretError("the secret could not be decrypted (wrong Windows account?)") from None


def is_encrypted(path: Path) -> bool:
    try:
        return Path(path).read_text(encoding="utf-8").startswith(MARKER)
    except OSError:
        return False
