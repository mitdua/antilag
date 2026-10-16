"""Settings the panel remembers between runs.

Stored in ``%APPDATA%\\antilag\\config.json`` (or ``~/.config/antilag`` outside
Windows). The secret is never stored in clear text: on Windows it is encrypted
with DPAPI, tied to the Windows user, the same way browsers protect their
passwords. Outside Windows it lives in a file with 0600 permissions.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict, dataclass, field


@dataclass
class SavedConfig:
    server: str = ""
    mode: str = "duplicate"
    primary: str | None = None
    links: list[str] = field(default_factory=list)
    secret: str = ""
    #: VPS player (from the access code). A config saved before there were
    #: several players does not have these: it is player 1.
    client_id: int = 1
    address: str = "10.66.0.2/24"
    name: str = ""


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "antilag")


def _protect(data: bytes) -> str:
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(data, encrypt=True)).decode()
    return "plain:" + base64.b64encode(data).decode()


def _unprotect(blob: str) -> str:
    scheme, _, payload = blob.partition(":")
    raw = base64.b64decode(payload)
    if scheme == "dpapi":
        if sys.platform != "win32":
            raise ValueError("secreto cifrado con DPAPI: solo se abre en el Windows que lo cifro")
        return _dpapi(raw, encrypt=False).decode()
    return raw.decode()


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    func = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = func(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI fallo")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def load(directory: str | None = None) -> SavedConfig:
    path = os.path.join(directory or config_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return SavedConfig()
    secret = ""
    if raw.get("secret"):
        try:
            secret = _unprotect(raw["secret"])
        except (OSError, ValueError):
            secret = ""  # another user or another machine: the code has to be pasted again
    return SavedConfig(
        server=raw.get("server", ""),
        mode=raw.get("mode", "duplicate"),
        primary=raw.get("primary"),
        links=list(raw.get("links", [])),
        secret=secret,
        client_id=int(raw.get("client_id", 1)),
        address=raw.get("address", "10.66.0.2/24"),
        name=raw.get("name", ""),
    )


def save(config: SavedConfig, directory: str | None = None) -> str:
    directory = directory or config_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "config.json")
    raw = asdict(config)
    raw["secret"] = _protect(config.secret.encode()) if config.secret else ""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
    os.replace(tmp, path)
    return path
