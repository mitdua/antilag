"""Windows smoke test, no administrator needed: DLL, ctypes, links and binding.

Usage, from the project root: python scripts\\smoke_windows.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from antilag.net.binding import make_udp_socket
from antilag.net.discovery import discover
from antilag.tun.wintun import WintunDevice, WintunError, find_dll

print("dll:", find_dll())
try:
    WintunDevice().close()
    print("adaptador creado (¿corriendo como administrador?)")
except WintunError as exc:
    print("sin admin, error esperado:", exc)
for link in discover():
    sock, strategy = make_udp_socket(link)
    sock.close()
    print("enlace:", link.describe(), "| fijacion:", strategy)
