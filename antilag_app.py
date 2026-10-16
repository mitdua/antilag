"""Entry point of the Windows executable (used by scripts/build_exe.ps1).

    antilag.exe            the panel
    antilag.exe --demo     simulated links, no VPS or administrator needed
"""

import sys

from antilag.ui.app import run


def _value(flag: str) -> str | None:
    if flag in sys.argv:
        index = sys.argv.index(flag)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return None


if __name__ == "__main__":
    run(
        demo="--demo" in sys.argv,
        screenshot=_value("--screenshot"),
        after=float(_value("--after") or 8),
    )
