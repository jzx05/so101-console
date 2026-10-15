"""Calibration file paths — mirrors the upstream rule so both tools agree on the file.

Corresponds to `src/lerobot/utils/constants.py:88-94`. Deliberately does not import
lerobot, which would pull in torch (see vendor/VENDOR.md). The rule is short enough
to copy; the cost is having to track upstream changes, which `tests/test_paths.py`
cross-checks against the real constants.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROBOTS = "robots"
TELEOPERATORS = "teleoperators"

#: Arm role -> subdirectory under the calibration root.
#: Matches ``SOFollower.name`` / ``SOLeader.name`` upstream.
ARM_DIRS: dict[str, str] = {
    "follower": "so_follower",
    "leader": "so_leader",
}

#: Arm role -> whether it lives under robots/ or teleoperators/.
ARM_ROOTS: dict[str, str] = {
    "follower": ROBOTS,
    "leader": TELEOPERATORS,
}


def _default_hf_home() -> Path:
    return Path(os.getenv("HF_HOME", "~/.cache/huggingface")).expanduser()


def lerobot_home() -> Path:
    """``HF_LEROBOT_HOME``, defaulting to ``$HF_HOME/lerobot``."""
    return Path(os.getenv("HF_LEROBOT_HOME", _default_hf_home() / "lerobot")).expanduser()


def calibration_root() -> Path:
    """``HF_LEROBOT_CALIBRATION``, defaulting to ``$HF_LEROBOT_HOME/calibration``."""
    return Path(os.getenv("HF_LEROBOT_CALIBRATION", lerobot_home() / "calibration")).expanduser()


def calibration_path(role: str, arm_id: str) -> Path:
    """Full path of an arm's calibration JSON.

    Args:
        role: ``"follower"`` or ``"leader"``.
        arm_id: The name given to the arm, e.g. ``"my_awesome_follower_arm"``.

    Returns:
        e.g. ``~/.cache/huggingface/lerobot/calibration/robots/so_follower/<id>.json``.
    """
    if role not in ARM_DIRS:
        raise ValueError(f"role must be one of {sorted(ARM_DIRS)}, got {role!r}")
    return calibration_root() / ARM_ROOTS[role] / ARM_DIRS[role] / f"{arm_id}.json"


def load_calibration_json(role: str, arm_id: str) -> dict[str, dict] | None:
    """Read a calibration file; ``None`` when it does not exist.

    No schema validation — a malformed file should be visible to the user rather
    than silently swallowed.
    """
    path = calibration_path(role, arm_id)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_calibration_json(role: str, arm_id: str, calibration: dict[str, dict]) -> Path:
    """Write a calibration file in the upstream format (4-space indent, fixed key order).

    Key order follows ``MotorCalibration``'s declaration order, so a diff against a
    file produced by ``lerobot-calibrate`` is byte-comparable.
    """
    path = calibration_path(role, arm_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {
        motor: {
            "id": int(cal["id"]),
            "drive_mode": int(cal.get("drive_mode", 0)),
            "homing_offset": int(cal["homing_offset"]),
            "range_min": int(cal["range_min"]),
            "range_max": int(cal["range_max"]),
        }
        for motor, cal in calibration.items()
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=4)
    return path
