"""Common options for launching system tools (PowerShell, netsh, ip).

In a windowed app without a console (the packaged executable), every console
child process opens its own black window. The panel's watcher calls
PowerShell every few seconds, so without this it would flicker all the time.
"""

from __future__ import annotations

import subprocess
import sys

#: pass as ``creationflags`` to subprocess
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
