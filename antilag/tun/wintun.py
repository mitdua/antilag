"""TUN interface on Windows on top of Wintun, WireGuard's driver.

Wintun is a signed DLL that installs its own driver the first time an adapter
is created; there is no separate installer. It is driven through ctypes, so
nothing needs compiling. Requires running as administrator.

The DLL is not in the repository: :func:`fetch_dll` downloads it from
wintun.net and checks the published hash before extracting it.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import platform
import sys
import urllib.request
import zipfile
from ctypes import wintypes

WINTUN_VERSION = "0.14.1"
WINTUN_URL = f"https://www.wintun.net/builds/wintun-{WINTUN_VERSION}.zip"
#: hash of the official zip; if it does not match, nothing is extracted
WINTUN_SHA256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"

#: Fixed GUID: Windows recognizes the adapter across runs and does not create
#: a new network profile ("Network 2", "Network 3"...) every time it starts.
ADAPTER_GUID = (0x7A1E1A66, 0x0D5B, 0x4A3C, (0x9C, 0x2E, 0x61, 0x6E, 0x74, 0x69, 0x6C, 0x61))

#: 4 MiB ring: plenty for game traffic and browsing
DEFAULT_CAPACITY = 0x400000

_ERROR_HANDLE_EOF = 38
_ERROR_BUFFER_OVERFLOW = 111
_ERROR_NO_MORE_ITEMS = 259
_WAIT_OBJECT_0 = 0


class WintunError(OSError):
    pass


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _arch_dir() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "amd64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine.startswith("arm"):
        return "arm"
    return "x86"


def default_dll_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wintun.dll")


def find_dll(explicit: str | None = None) -> str | None:
    """Where wintun.dll is: argument, ANTILAG_WINTUN, next to the package, or cwd."""
    candidates = [explicit, os.environ.get("ANTILAG_WINTUN"), default_dll_path(),
                  os.path.join(os.getcwd(), "wintun.dll")]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def fetch_dll(dest: str | None = None, url: str = WINTUN_URL) -> str:
    """Download the official zip, check its SHA-256 and extract this architecture's DLL."""
    dest = dest or default_dll_path()
    with urllib.request.urlopen(url, timeout=60) as response:
        blob = response.read()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != WINTUN_SHA256:
        raise WintunError(f"hash de wintun inesperado: {digest}; no se extrae nada")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        data = archive.read(f"wintun/bin/{_arch_dir()}/wintun.dll")
    with open(dest, "wb") as handle:
        handle.write(data)
    return dest


class WintunDevice:
    """Wintun adapter with an open session.

    ``recv`` is called from a single thread; ``write`` is safe from several,
    as Wintun documents for allocation and sending.
    """

    def __init__(
        self,
        name: str = "antilag",
        dll_path: str | None = None,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        if sys.platform != "win32":
            raise WintunError("Wintun solo existe en Windows")
        path = find_dll(dll_path)
        if path is None:
            raise WintunError("no se encontro wintun.dll; correr 'antilag fetch-wintun'")

        self._dll = dll = ctypes.WinDLL(path, use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()

        guid = _GUID(ADAPTER_GUID[0], ADAPTER_GUID[1], ADAPTER_GUID[2],
                     (ctypes.c_ubyte * 8)(*ADAPTER_GUID[3]))
        self._adapter = dll.WintunCreateAdapter(name, "antilag", ctypes.byref(guid))
        if not self._adapter:
            raise WintunError(ctypes.get_last_error(), "WintunCreateAdapter fallo; hace falta correr como administrador")
        self.name = name

        luid = ctypes.c_uint64()
        dll.WintunGetAdapterLUID(self._adapter, ctypes.byref(luid))
        index = wintypes.ULONG()
        iphlpapi = ctypes.WinDLL("iphlpapi")
        status = iphlpapi.ConvertInterfaceLuidToIndex(ctypes.byref(luid), ctypes.byref(index))
        if status != 0:
            dll.WintunCloseAdapter(self._adapter)
            raise WintunError(status, "no se pudo obtener el indice del adaptador")
        self.index = index.value

        self._session = dll.WintunStartSession(self._adapter, capacity)
        if not self._session:
            error = ctypes.get_last_error()
            dll.WintunCloseAdapter(self._adapter)
            raise WintunError(error, "WintunStartSession fallo")
        self._event = dll.WintunGetReadWaitEvent(self._session)
        self.dropped = 0

    def _bind(self) -> None:
        dll, k32 = self._dll, self._kernel32
        handle = ctypes.c_void_p
        signatures = {
            "WintunCreateAdapter": (handle, [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]),
            "WintunCloseAdapter": (None, [handle]),
            "WintunGetAdapterLUID": (None, [handle, ctypes.c_void_p]),
            "WintunStartSession": (handle, [handle, wintypes.DWORD]),
            "WintunEndSession": (None, [handle]),
            "WintunGetReadWaitEvent": (wintypes.HANDLE, [handle]),
            "WintunReceivePacket": (ctypes.c_void_p, [handle, ctypes.POINTER(wintypes.DWORD)]),
            "WintunReleaseReceivePacket": (None, [handle, ctypes.c_void_p]),
            "WintunAllocateSendPacket": (ctypes.c_void_p, [handle, wintypes.DWORD]),
            "WintunSendPacket": (None, [handle, ctypes.c_void_p]),
        }
        for name, (restype, argtypes) in signatures.items():
            func = getattr(dll, name)
            func.restype = restype
            func.argtypes = argtypes
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    def _try_receive(self) -> bytes | None:
        size = wintypes.DWORD()
        ptr = self._dll.WintunReceivePacket(self._session, ctypes.byref(size))
        if ptr:
            try:
                return ctypes.string_at(ptr, size.value)
            finally:
                self._dll.WintunReleaseReceivePacket(self._session, ptr)
        error = ctypes.get_last_error()
        if error == _ERROR_NO_MORE_ITEMS:
            return None
        if error == _ERROR_HANDLE_EOF:
            raise EOFError("el adaptador Wintun se esta cerrando")
        raise WintunError(error, "WintunReceivePacket fallo")

    def recv(self, timeout: float) -> bytes | None:
        """One packet, or None if none arrives within ``timeout`` seconds."""
        packet = self._try_receive()
        if packet is not None:
            return packet
        if self._kernel32.WaitForSingleObject(self._event, int(timeout * 1000)) != _WAIT_OBJECT_0:
            return None
        return self._try_receive()

    def write(self, packet: bytes) -> None:
        ptr = self._dll.WintunAllocateSendPacket(self._session, len(packet))
        if not ptr:
            if ctypes.get_last_error() == _ERROR_BUFFER_OVERFLOW:
                self.dropped += 1  # ring full: drop it, as a router would
                return
            raise WintunError(ctypes.get_last_error(), "WintunAllocateSendPacket fallo")
        ctypes.memmove(ptr, packet, len(packet))
        self._dll.WintunSendPacket(self._session, ptr)

    def close(self) -> None:
        if self._session:
            self._dll.WintunEndSession(self._session)
            self._session = None
        if self._adapter:
            # Closing the last handle removes the adapter, and its routes with it.
            self._dll.WintunCloseAdapter(self._adapter)
            self._adapter = None
