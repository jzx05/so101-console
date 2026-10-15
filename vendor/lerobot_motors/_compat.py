"""Stand-ins for the few ``lerobot.utils`` helpers the vendored motor layer needs.

Upstream these live in ``lerobot.utils.import_utils`` and ``lerobot.utils.utils``.
We cannot import them: ``lerobot/utils/__init__.py`` unconditionally pulls in
``device_utils``, which does a bare ``import torch``. Vendoring lets the debug
console run in a torch-free environment.

Keep this module dependency-free apart from the stdlib -- the whole point is that
importing the motor layer must not drag anything heavy along.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

__all__ = [
    "_deepdiff_available",
    "_feetech_sdk_available",
    "_serial_available",
    "enter_pressed",
    "move_cursor_up",
    "require_package",
]


def _has(module_name: str) -> bool:
    """Return True if ``module_name`` can be found, without importing it."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


# Mirrors the three flags that lerobot's motor layer imports.
_serial_available = _has("serial")
_deepdiff_available = _has("deepdiff")
_feetech_sdk_available = _has("scservo_sdk")

_require_package_cache: dict[str, bool] = {}


def require_package(pkg_name: str, extra: str = "", import_name: str | None = None) -> None:
    """Raise an informative ImportError if an optional dependency is missing.

    Args:
        pkg_name: Distribution name, e.g. ``"feetech-servo-sdk"``.
        extra: Unused here; kept so call sites stay byte-identical to upstream.
        import_name: Module name when it differs from ``pkg_name``.
    """
    cache_key = import_name or pkg_name
    if cache_key not in _require_package_cache:
        _require_package_cache[cache_key] = _has(cache_key)
    if not _require_package_cache[cache_key]:
        # The interpreter running this process is always the right one to install into,
        # so point at it rather than at a hard-coded venv path (which differs between
        # Windows' Scripts/ and Linux's bin/).
        raise ImportError(
            f"'{pkg_name}' is required but not installed. Install it with:\n"
            f"    uv pip install --python {sys.executable} {pkg_name}"
        )


def enter_pressed() -> bool:
    """Return True if the user pressed Enter on stdin (non-blocking).

    Copied from ``lerobot.utils.utils``. The debug console never relies on this --
    it drives sweeps from the browser -- but the vendored
    ``record_ranges_of_motion`` still references it.
    """
    if sys.platform == "win32":
        import msvcrt

        if msvcrt.kbhit():
            key = msvcrt.getch()
            return key in (b"\r", b"\n")  # enter key
        return False
    else:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(ready) and sys.stdin.readline().strip() == ""


def move_cursor_up(lines: int) -> None:
    """Move the terminal cursor up by ``lines``."""
    print(f"\033[{lines}A", end="")
