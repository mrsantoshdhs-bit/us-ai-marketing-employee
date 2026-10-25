"""Encrypt secrets at rest with Windows DPAPI (current-user scope, no extra dependency or cost).

Values are stored as "dpapi:<base64>". On non-Windows hosts the value is stored as "plain:<value>"
and a warning is logged; restrict file permissions on those hosts. Legacy unprefixed values still read.
"""

from __future__ import annotations

import base64
import logging
import os

log = logging.getLogger("aime.secure_store")
_ENTROPY = b"aime-oauth-v1"

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                                            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _UI_FORBIDDEN = 0x1

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _dpapi(data: bytes, encrypt: bool) -> bytes:
        blob_in, _keep_in = _blob(data)
        entropy, _keep_e = _blob(_ENTROPY)
        out = _Blob()
        fn = _crypt32.CryptProtectData if encrypt else _crypt32.CryptUnprotectData
        desc = "aime" if encrypt else None
        if not fn(ctypes.byref(blob_in), desc, ctypes.byref(entropy), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(value: str) -> str:
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")
    log.warning("DPAPI unavailable on this OS; storing secret without encryption")
    return "plain:" + value


def unprotect(stored: str) -> str:
    if stored.startswith("dpapi:"):
        if os.name != "nt":
            raise RuntimeError("Secret was encrypted with Windows DPAPI on another machine; reconnect the integration")
        return _dpapi(base64.b64decode(stored[6:]), False).decode("utf-8")
    if stored.startswith("plain:"):
        return stored[6:]
    return stored  # legacy plaintext


def is_protected(stored: str | None) -> bool:
    return bool(stored) and stored.startswith("dpapi:")
