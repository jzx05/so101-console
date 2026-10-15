"""Vendored copy of lerobot's Feetech motor layer, free of the torch dependency.

Upstream ``lerobot.utils.__init__`` imports ``device_utils`` which does a bare
``import torch``, so importing *any* lerobot hardware module drags torch in. The
debug console only needs the serial protocol, so the files are copied here and
the handful of ``lerobot.utils.*`` imports are retargeted at :mod:`._compat`.

See ``vendor/VENDOR.md`` for the upstream commit and the exact edits.

Public surface mirrors ``lerobot.motors`` plus the Feetech bus::

    from vendor.lerobot_motors import Motor, MotorCalibration, MotorNormMode
    from vendor.lerobot_motors.feetech import FeetechMotorsBus, OperatingMode
"""

from .motors_bus import (
    Motor,
    MotorCalibration,
    MotorNormMode,
    MotorsBus,
    SerialMotorsBus,
)

__all__ = [
    "Motor",
    "MotorCalibration",
    "MotorNormMode",
    "MotorsBus",
    "SerialMotorsBus",
]
