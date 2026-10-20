"""API keys kept on this Windows PC, encrypted for the signed-in Windows user (user decision 2026-10-20).

The user found pasting both keys at every start tedious and chose to keep them on the PC, encrypted. Windows'
own data protection (DPAPI, CryptProtectData) encrypts them with the Windows account's secret: only the same
user on the same PC can decrypt them, and the file is useless if copied elsewhere. The file sits next to the
records (prototype/data), so the automatic update keeps it. Other systems (Mac) do not store keys: they are
typed at every start, as before.

The keys can be replaced (the user may change APIs later): the desktop file 「APIキーを入れ直す」 starts with
AI_CONCIERGE_FORGET_KEYS=1, which deletes this file before the keys are asked.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys
from typing import Callable

from ..reception.store import DEFAULT_DB

PATH = DEFAULT_DB.parent / "api-keys.dpapi.json"
ENTROPY = b"AI-Concierge api keys v1"   # an extra input to DPAPI, so other programs of this user cannot read it

Crypt = Callable[[bytes], bytes]


def available() -> bool:
    return sys.platform == "win32"


def _dpapi(protect: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes) -> tuple[Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    inp, _keep_in = blob(data)
    ent, _keep_ent = blob(ENTROPY)
    out = Blob()
    ui_forbidden = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inp), ctypes.c_wchar_p("AI-Concierge"), ctypes.byref(ent),
                                      None, None, ui_forbidden, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent), None, None, ui_forbidden,
                                        ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI error {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    return _dpapi(True, data)


def unprotect(data: bytes) -> bytes:
    return _dpapi(False, data)


def load(path: pathlib.Path = PATH, decrypt: Crypt = unprotect) -> dict[str, str]:
    """The saved keys. A file that cannot be read (another user, a damaged file) counts as no saved keys."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return {name: decrypt(base64.b64decode(blob)).decode("utf-8") for name, blob in saved["keys"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def save(keys: dict[str, str], path: pathlib.Path = PATH, encrypt: Crypt = protect) -> None:
    payload = {"note": "APIキー（Windowsの暗号化。このPCのこのユーザーだけが読めます）",
               "keys": {name: base64.b64encode(encrypt(value.encode("utf-8"))).decode("ascii")
                        for name, value in keys.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def forget(path: pathlib.Path = PATH) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
