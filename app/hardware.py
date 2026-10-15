"""Serial and arm plumbing. Depends only on the vendored motor layer.

This layer deliberately does **not** use lerobot's ``SOFollower`` / ``SOLeader``
device classes: their ``connect()`` cascades into ``calibrate()``, which blocks on
``input()`` at four call sites (``so_follower.py:140,155``, ``so_leader.py:108,123``).
A browser has no stdin to press Enter on.

Everything is built from bus primitives instead. Normalization, sign-magnitude
encoding and sync reads/writes still come from the vendored upstream code — none
of the protocol is reimplemented here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from vendor.lerobot_motors import Motor, MotorCalibration, MotorNormMode
from vendor.lerobot_motors.feetech import FeetechMotorsBus, OperatingMode

logger = logging.getLogger(__name__)

#: Model number the STS3215 reports.
STS3215_MODEL_NUMBER = 777

#: SO-101 joint layout, identical to upstream `so_follower.py:75-81`.
#: Body joints use DEGREES and the gripper uses RANGE_0_100 — that difference is
#: exactly why the leader/follower comparison has to skip the gripper.
_BODY_NORM = MotorNormMode.DEGREES
SO101_LAYOUT: dict[str, tuple[int, MotorNormMode]] = {
    "shoulder_pan": (1, _BODY_NORM),
    "shoulder_lift": (2, _BODY_NORM),
    "elbow_flex": (3, _BODY_NORM),
    "wrist_flex": (4, _BODY_NORM),
    "wrist_roll": (5, _BODY_NORM),
    "gripper": (6, MotorNormMode.RANGE_0_100),
}

#: Motor ids we expect to find on a healthy SO-101 bus.
EXPECTED_MODELS: dict[int, int] = {
    mid: STS3215_MODEL_NUMBER for mid, _ in SO101_LAYOUT.values()
}


def build_motors() -> dict[str, Motor]:
    """Build the SO-101 motor table."""
    return {name: Motor(mid, "sts3215", norm) for name, (mid, norm) in SO101_LAYOUT.items()}


def ascii_only(text: str | None) -> str:
    """Return ``text`` if it is pure ASCII, otherwise an empty string.

    Port descriptions and OS error messages come back in the system locale, so a
    Chinese Windows returns things like ``通信端口 (COM1)`` or ``拒绝访问。``. The
    interface is English-only, so localized strings are dropped at the source
    rather than leaking into tables and error banners.
    """
    if not text:
        return ""
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return ""
    return text


#: PNP ids of built-in serial ports. Windows names these in the system locale, so
#: a Chinese install reports "通信端口 (COM1)". The PNP id is locale-independent, so
#: the English name is derived from it instead.
_BUILTIN_NAMES = {
    "PNP0500": "Communications Port (standard UART)",
    "PNP0501": "Communications Port (16550 UART)",
}


def pnp_id(hwid: str) -> str:
    """Extract the PNP id from a hardware id: ``ACPI\\PNP0501\\0`` -> ``PNP0501``."""
    for part in (hwid or "").upper().split("\\"):
        if part.startswith("PNP"):
            return part
    return ""


def port_description(hwid: str, os_description: str | None) -> str:
    """Best English description for a port.

    The driver's own text is used when it is already ASCII. Built-in ports report
    a localized name, so those fall back to a name derived from the PNP id. An
    unknown localized name yields an empty string rather than guesswork — the Kind
    column and the hardware id still identify the port.

    Args:
        hwid: Hardware id, e.g. ``ACPI\\PNP0501\\0``.
        os_description: What the OS reports, possibly in another language.

    Returns:
        An ASCII description, or ``""``.
    """
    ascii_desc = ascii_only(os_description)
    if ascii_desc:
        return ascii_desc
    return _BUILTIN_NAMES.get(pnp_id(hwid), "")


def port_kind(hwid: str) -> str:
    """Classify a port from its hardware id.

    Locale-independent, and more useful than the driver's description: a built-in
    motherboard UART can never drive a Feetech bus, because it cannot reach
    1 Mbaud. That is worth knowing before a probe fails.

    Args:
        hwid: The hardware id string, e.g. ``USB\\VID_1A86&PID_55D3\\...``.

    Returns:
        ``"USB adapter"``, ``"Built-in port"`` or ``"Serial port"``.
    """
    prefix = hwid.upper().split("\\", 1)[0]
    if prefix.startswith("USB"):
        return "USB adapter"
    # Built-in ports show up as "ACPI\PNP0501\..." or, on some systems, bare "PNP0501".
    if prefix == "ACPI" or prefix.startswith("PNP"):
        return "Built-in port"
    return "Serial port"


def list_serial_ports() -> list[dict]:
    """List local serial ports with a locale-independent kind and description.

    There are two CH343 adapters on a typical SO-101 setup, and the COM number
    alone does not say which arm is which — the serial number does (follower
    ``...8534``, leader ``...8639`` on this machine).
    """
    from serial.tools import list_ports

    return [
        {
            "device": p.device,
            "kind": port_kind(p.hwid or ""),
            "description": port_description(p.hwid or "", p.description),
            "hwid": p.hwid or "",
            "serial_number": p.serial_number or "",
        }
        for p in sorted(list_ports.comports(), key=lambda p: p.device)
    ]


def enumerate_motors(bus: FeetechMotorsBus) -> dict[int, int]:
    """Ping each motor individually; returns ``{motor_id: model_number}``.

    **Do not use ``broadcast_ping`` for this.** A broadcast asks every motor to
    answer at once, so the replies collide on the bus and the adapter only ever
    catches some of them — and each silent motor costs a full timeout on top.
    Measured with both arms online:

        broadcast_ping  ->  0-1 motors, 1576 ms
        per-motor ping  ->  6 motors,      5 ms

    Upstream ``_assert_motors_exist`` pings individually too (``motors_bus.py:468``).
    """
    found: dict[int, int] = {}
    for name, motor in bus.motors.items():
        model = bus.ping(name)
        if model is not None:
            found[motor.id] = int(model)
    return found


def read_calibration_safe(bus: FeetechMotorsBus) -> tuple[dict[str, dict], dict[str, str]]:
    """Read the calibration registers motor by motor; one dead motor is tolerated.

    Upstream ``read_calibration()`` (``feetech.py:247``) is all-or-nothing: the
    first motor that does not answer raises ``ConnectionError``. On 2026-10-15 the
    gripper (ID 6) dropped off the bus, which would have taken the entire health
    check down — exactly when it was needed most.

    Args:
        bus: A connected bus.

    Returns:
        ``(calibration_by_motor, motor_name -> error_text)``
    """
    out: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for name, motor in bus.motors.items():
        try:
            out[name] = {
                "id": motor.id,
                "drive_mode": 0,
                "homing_offset": int(bus.read("Homing_Offset", name, normalize=False, num_retry=1)),
                "range_min": int(bus.read("Min_Position_Limit", name, normalize=False, num_retry=1)),
                "range_max": int(bus.read("Max_Position_Limit", name, normalize=False, num_retry=1)),
            }
        except Exception as exc:  # noqa: BLE001 - any comms failure should only affect this motor
            errors[name] = f"{type(exc).__name__}: {exc}"
    return out, errors


@dataclass
class ProbeResult:
    """Outcome of one port probe."""

    port: str
    ok: bool
    motors: dict[int, int]
    error: str | None = None
    elapsed_ms: float = 0.0


def explain_open_failure(port: str) -> str:
    """Explain why a port would not open at the Feetech baudrate.

    Upstream's message ("Could not connect on port ... Try running
    ``lerobot-find-port``") is both raw and CLI-oriented, and it hides the usual
    cause. Opening at a low baudrate first separates the two cases:

    * opens at 115200 but not at 1 Mbaud — a built-in motherboard UART, which
      cannot reach that speed. Expect this on COM1, and it is not a fault.
    * does not open at all — busy, or the port does not exist.

    Args:
        port: The serial port that failed.

    Returns:
        A one-line explanation for the user.
    """
    import serial

    try:
        probe = serial.Serial(port, 115200, timeout=0.1)
        probe.close()
    except Exception:  # noqa: BLE001 - any failure here means "cannot open at all"
        return (
            f"'{port}' could not be opened. Another program may already be using it, "
            "or the port does not exist."
        )
    return (
        f"'{port}' opens at low speed but not at the Feetech baudrate (1 Mbaud). "
        "A built-in motherboard serial port cannot reach that speed — the arms "
        "connect through a USB adapter (CH343), which appears as a separate COM port."
    )


def probe_port(port: str, timeout_ms: int = 500) -> ProbeResult:
    """Report which motors answer on a port, without the handshake check.

    Two things matter here:

    1. ``connect(handshake=False)`` — the default ``connect()`` runs
       ``_assert_motors_exist()``, which raises ``RuntimeError`` as soon as one
       motor is missing. A health check wants to be *told* which one is missing,
       not to blow up.
    2. Enumerate with per-motor pings, never ``broadcast_ping`` — see
       :func:`enumerate_motors`.

    Args:
        port: Serial port name, e.g. ``"COM24"``.
        timeout_ms: Per-transaction timeout; a silent motor costs about this much.

    Returns:
        The probe result; on failure ``ok=False`` and ``error`` explains why.
    """
    start = time.perf_counter()
    bus = FeetechMotorsBus(port=port, motors=build_motors())
    try:
        bus.set_timeout(timeout_ms)
        try:
            bus.connect(handshake=False)
        except Exception:  # noqa: BLE001 - translate upstream's raw text into a reason
            return ProbeResult(
                port=port,
                ok=False,
                motors={},
                error=explain_open_failure(port),
                elapsed_ms=(time.perf_counter() - start) * 1e3,
            )
        return ProbeResult(
            port=port,
            ok=True,
            motors=enumerate_motors(bus),
            elapsed_ms=(time.perf_counter() - start) * 1e3,
        )
    except Exception as exc:  # noqa: BLE001 - anything past the open step
        detail = ascii_only(str(exc))  # OS messages arrive localized
        return ProbeResult(
            port=port,
            ok=False,
            motors={},
            error=f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
            elapsed_ms=(time.perf_counter() - start) * 1e3,
        )
    finally:
        try:
            bus.disconnect(disable_torque=False)
        except Exception:  # noqa: BLE001 - already closed, or never opened
            pass


class ArmSession:
    """One arm's session: bus + port + name + the calibration file on disk.

    Unrelated to lerobot's ``Robot`` / ``Teleoperator`` classes; it borrows only
    their data layout (joint names, ids, JSON shape) so that the calibration files
    written here are interchangeable with ``lerobot-calibrate``.
    """

    def __init__(self, role: str, port: str, arm_id: str):
        """
        Args:
            role: ``"follower"`` or ``"leader"``.
            port: Serial port name.
            arm_id: Arm name; determines the calibration filename.
        """
        if role not in ("follower", "leader"):
            raise ValueError(f"role must be follower / leader, got {role!r}")
        self.role = role
        self.port = port
        self.arm_id = arm_id
        self.bus: FeetechMotorsBus | None = None
        self._homing_offsets: dict[str, int] = {}

    # ---------------------------------------------------------------- lifecycle

    def connect(self) -> dict[int, int]:
        """Open the port and enumerate motors, skipping the handshake check.

        Returns:
            ``{motor_id: model_number}`` for every motor that answered.

        Raises:
            ConnectionError: the port could not be opened.
        """
        if self.bus is not None:
            raise RuntimeError(f"{self.role} is already connected on {self.port}")
        bus = FeetechMotorsBus(port=self.port, motors=build_motors())
        bus.connect(handshake=False)
        self.bus = bus
        return enumerate_motors(bus)

    def disconnect(self) -> None:
        """Disconnect and **make sure torque is off** — otherwise the arm holds position live."""
        bus, self.bus = self.bus, None
        if bus is None:
            return
        try:
            if bus.is_connected:
                bus.disconnect(disable_torque=True)
        except Exception:  # noqa: BLE001 - teardown errors should not bubble
            logger.warning("%s: error while disconnecting", self.role, exc_info=True)

    @property
    def connected(self) -> bool:
        return self.bus is not None and self.bus.is_connected

    def _require(self) -> FeetechMotorsBus:
        if self.bus is None or not self.bus.is_connected:
            raise RuntimeError(f"{self.role} is not connected")
        return self.bus

    # ---------------------------------------------------------------- reads

    def read_positions(self, normalize: bool = False) -> dict[str, int | float]:
        """Read every joint's current position."""
        return dict(self._require().sync_read("Present_Position", normalize=normalize))

    def read_calibration(self) -> tuple[dict[str, dict], dict[str, str]]:
        """Read the calibration registers, tolerating individual dead motors."""
        return read_calibration_safe(self._require())

    # ---------------------------------------------------------------- torque

    def set_torque(self, enabled: bool) -> None:
        """Enable or release torque on every motor."""
        bus = self._require()
        if enabled:
            bus.enable_torque()
        else:
            bus.disable_torque()

    def torque_enabled(self) -> bool | None:
        """True if any motor has torque on; ``None`` if nothing could be read."""
        bus = self._require()
        states = []
        for name in bus.motors:
            try:
                states.append(int(bus.read("Torque_Enable", name, normalize=False, num_retry=1)) != 0)
            except Exception:  # noqa: BLE001 - skip unreadable motors
                continue
        return any(states) if states else None

    # ---------------------------------------------------------------- writes

    def apply_calibration(self, calibration: dict[str, dict], motors: list[str] | None = None) -> None:
        """Write calibration values into the motor registers.

        Must run inside ``torque_disabled()``: ``enable_torque()`` writes ``Lock=1``,
        and ``Homing_Offset`` / ``Min_Position_Limit`` / ``Max_Position_Limit`` live
        in EEPROM, where a locked write is silently dropped.

        Args:
            calibration: Motor name -> calibration dict.
            motors: Subset to write; ``None`` writes them all.
        """
        bus = self._require()
        names = motors if motors is not None else list(calibration)
        with bus.torque_disabled():
            for name in names:
                cal = calibration[name]
                bus.write("Homing_Offset", name, int(cal["homing_offset"]))
                bus.write("Min_Position_Limit", name, int(cal["range_min"]))
                bus.write("Max_Position_Limit", name, int(cal["range_max"]))
        # Keep the bus cache in step so later normalized reads use the new values.
        bus.calibration = {
            name: MotorCalibration(
                id=bus.motors[name].id,
                drive_mode=int(cal.get("drive_mode", 0)),
                homing_offset=int(cal["homing_offset"]),
                range_min=int(cal["range_min"]),
                range_max=int(cal["range_max"]),
            )
            for name, cal in calibration.items()
        }

    def prepare_for_calibration(self) -> None:
        """Calibration step 1: release torque and switch every motor to position mode."""
        bus = self._require()
        bus.disable_torque()
        for name in bus.motors:
            bus.write("Operating_Mode", name, OperatingMode.POSITION.value)

    def set_half_turn_homings(self) -> dict[str, int]:
        """Make the current pose each joint's range midpoint (reads back as 2047).

        Note that upstream clears ``bus.calibration`` inside this call via
        ``reset_calibration()``, so **every read afterwards must use
        ``normalize=False``** or it raises
        ``RuntimeError: ... has no calibration registered``.

        Returns:
            Motor name -> the homing offset that was written.
        """
        bus = self._require()
        offsets = bus.set_half_turn_homings()
        self._homing_offsets = {k: int(v) for k, v in offsets.items()}
        return dict(self._homing_offsets)

    @property
    def homing_offsets(self) -> dict[str, int]:
        return dict(self._homing_offsets)


def build_calibration(
    mins: dict[str, int],
    maxes: dict[str, int],
    homing_offsets: dict[str, int],
) -> dict[str, dict]:
    """Assemble a calibration table in the upstream JSON shape.

    Args:
        mins: Motor name -> smallest observed position.
        maxes: Motor name -> largest observed position.
        homing_offsets: Motor name -> offset written by ``set_half_turn_homings``.

    Returns:
        A dict ready for :func:`paths.save_calibration_json` or
        :meth:`ArmSession.apply_calibration`.
    """
    return {
        name: {
            "id": SO101_LAYOUT[name][0],
            "drive_mode": 0,
            "homing_offset": int(homing_offsets.get(name, 0)),
            "range_min": int(mins[name]),
            "range_max": int(maxes[name]),
        }
        for name in SO101_LAYOUT
    }


