#!/usr/bin/env python
"""Re-sync the vendored motor layer from a lerobot checkout.

    .venv\\Scripts\\python.exe vendor\\sync_vendor.py

Three steps:

1. Copy the 7 files out of ``<lerobot>/src/lerobot/``, preserving the layout.
2. Apply the three ``lerobot.utils.*`` import patches.
3. Run the "no torch" check.

The lerobot checkout is found automatically when this repo is cloned into
``<lerobot>/tools/debug_console`` (the normal setup). Otherwise point at one with
``--lerobot`` or the ``LEROBOT_SRC`` environment variable.

Patching is **strict**: if a line is not found exactly once, the whole run fails
rather than silently producing a broken vendored copy. When upstream refactors
those imports you have to look at it and decide — which is the intended behaviour.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = TOOL_DIR / "vendor" / "lerobot_motors"

# (path relative to upstream, path relative to the vendor directory)
FILES: list[tuple[str, str]] = [
    ("src/lerobot/motors/motors_bus.py", "motors_bus.py"),
    ("src/lerobot/motors/encoding_utils.py", "encoding_utils.py"),
    ("src/lerobot/motors/feetech/feetech.py", "feetech/feetech.py"),
    ("src/lerobot/motors/feetech/tables.py", "feetech/tables.py"),
    ("src/lerobot/motors/feetech/__init__.py", "feetech/__init__.py"),
    ("src/lerobot/utils/errors.py", "errors.py"),
    ("src/lerobot/utils/decorators.py", "decorators.py"),
]

# (file in vendor, original text, replacement). The original must occur exactly once.
PATCHES: list[tuple[str, str, str]] = [
    (
        "motors_bus.py",
        "from lerobot.utils.import_utils import _deepdiff_available, _serial_available, require_package",
        "from ._compat import _deepdiff_available, _serial_available, require_package",
    ),
    (
        "motors_bus.py",
        "from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected\n"
        "from lerobot.utils.utils import enter_pressed, move_cursor_up",
        "from ._compat import enter_pressed, move_cursor_up\n"
        "from .decorators import check_if_already_connected, check_if_not_connected",
    ),
    (
        "feetech/feetech.py",
        "from lerobot.utils.import_utils import _feetech_sdk_available, require_package",
        "from .._compat import _feetech_sdk_available, require_package",
    ),
]

# Hand-written files. A re-sync must never delete them.
PRESERVED = ["_compat.py", "__init__.py"]


def find_lerobot_src(explicit: str | None = None) -> Path:
    """Locate a lerobot checkout to copy the motor layer out of.

    This repo normally sits at ``<lerobot>/tools/debug_console``, so the checkout
    is two levels up. Anywhere else, point at it explicitly.

    Args:
        explicit: ``--lerobot`` value, or ``None``.

    Returns:
        The lerobot checkout root.

    Raises:
        SystemExit: No checkout was found at any of the candidate locations.
    """
    candidates: list[tuple[str, Path]] = []
    if explicit:
        candidates.append(("--lerobot", Path(explicit).expanduser()))
    env = os.environ.get("LEROBOT_SRC")
    if env:
        candidates.append(("LEROBOT_SRC", Path(env).expanduser()))
    candidates.append(("parent of this repo", TOOL_DIR.parent.parent))

    for label, root in candidates:
        if (root / "src" / "lerobot" / "motors").is_dir():
            print(f"  lerobot source: {root}  (from {label})")
            return root
        # A path pointing straight at src/lerobot is also fine.
        if (root / "motors" / "motors_bus.py").is_file():
            print(f"  lerobot source: {root}  (from {label})")
            return root.parent.parent

    tried = "\n".join(f"    {label}: {root}" for label, root in candidates)
    raise SystemExit(
        "could not find a lerobot checkout. Tried:\n"
        f"{tried}\n"
        "Pass one explicitly:  sync_vendor.py --lerobot C:\\path\\to\\lerobot"
    )


def sync(lerobot_src: Path) -> None:
    """Copy, patch, and verify."""
    if not (lerobot_src / "src" / "lerobot").is_dir():
        raise SystemExit(f"not a lerobot checkout (no src/lerobot): {lerobot_src}")

    for up_rel, vend_rel in FILES:
        src = lerobot_src / up_rel
        dst = VENDOR_DIR / vend_rel
        if not src.is_file():
            raise SystemExit(f"upstream file missing: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  copied {up_rel}  ->  {vend_rel}")

    for vend_rel, old, new in PATCHES:
        path = VENDOR_DIR / vend_rel
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"patch failed: the target text occurs {count} times in {vend_rel} (expected 1).\n"
                f"Upstream probably changed the import. Review and update PATCHES:\n{old!r}"
            )
        path.write_text(text.replace(old, new), encoding="utf-8")
        print(f"  patched {vend_rel}")

    for name in PRESERVED:
        if not (VENDOR_DIR / name).is_file():
            raise SystemExit(f"missing hand-written file {name}; a sync must not remove it")

    print("\nsync done, verifying…\n")
    verify()


def verify() -> None:
    """Confirm the vendored copy imports in a torch-free environment."""
    code = (
        "import sys;"
        "from vendor.lerobot_motors import Motor, MotorCalibration, MotorNormMode;"
        "from vendor.lerobot_motors.feetech import FeetechMotorsBus, OperatingMode;"
        "heavy=[m for m in ('torch','pandas','pyarrow','draccus','huggingface_hub') if m in sys.modules];"
        "assert not heavy, f'pulled in: {heavy}';"
        "print('  OK: imports cleanly without torch')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=TOOL_DIR, capture_output=True, text=True
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("verification failed — the vendored copy still pulls heavy deps")


def main() -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Re-sync the vendored lerobot motor layer")
    parser.add_argument("--verify-only", action="store_true", help="run the check without copying")
    parser.add_argument(
        "--lerobot",
        metavar="PATH",
        help="lerobot checkout to copy from (default: the repo two levels up, "
             "or $LEROBOT_SRC)",
    )
    args = parser.parse_args()
    if args.verify_only:
        verify()
    else:
        sync(find_lerobot_src(args.lerobot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
