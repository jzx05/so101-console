"""Worker state machine — exercised against a fake bus, no hardware.

The focus is a bug hit in production on 2026-10-15: starting teleop immediately
raised ``FeetechMotorsBus(...) has no calibration registered``. Upstream found an
empty ``bus.calibration`` while normalizing a position read and printed the entire
motor table as the error message, which said nothing about what to do next.
"""

from __future__ import annotations

import json

import pytest

from app import worker as worker_mod
from app.worker import ConsoleCore
from vendor.lerobot_motors import MotorCalibration

CAL = {
    name: {"id": i + 1, "drive_mode": 0, "homing_offset": 0,
           "range_min": 800, "range_max": 3200}
    for i, name in enumerate(
        ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"])
}


class FakeMotor:
    def __init__(self, mid):
        self.id = mid


class FakeBus:
    """Just enough bus: tracks torque, and its calibration cache can be cleared."""

    def __init__(self, fail_reads: set[str] | None = None):
        self.motors = {n: FakeMotor(c["id"]) for n, c in CAL.items()}
        self.calibration: dict = {}
        self.torque = False
        self.is_connected = True
        self.fail_reads = fail_reads or set()
        self.writes: list[tuple] = []
        self.goal_writes: list[dict] = []

    def read(self, data_name, motor, normalize=True, num_retry=0):
        if motor in self.fail_reads:
            raise ConnectionError(f"{motor} did not respond")
        return {
            "Homing_Offset": CAL[motor]["homing_offset"],
            "Min_Position_Limit": CAL[motor]["range_min"],
            "Max_Position_Limit": CAL[motor]["range_max"],
            "Torque_Enable": 1 if self.torque else 0,
        }[data_name]

    def write(self, data_name, motor, value, normalize=True, num_retry=0):
        self.writes.append((data_name, motor, value))

    def sync_read(self, data_name, motors=None, normalize=True, num_retry=0):
        if not self.calibration and normalize:
            raise RuntimeError(f"{self} has no calibration registered.")
        names = motors if motors is not None else list(self.motors)
        return {n: 0.0 for n in names}

    def sync_write(self, data_name, values, normalize=True, num_retry=0):
        self.goal_writes.append(dict(values))

    def disable_torque(self, motors=None, num_retry=0):
        self.torque = False

    def enable_torque(self, motors=None, num_retry=0):
        self.torque = True

    def disconnect(self, disable_torque=True):
        self.torque = False
        self.is_connected = False


class FakeSession:
    def __init__(self, role, bus: FakeBus):
        self.role = role
        self.port = "COM_FAKE"
        self.arm_id = "fake"
        self.bus = bus

    connected = property(lambda self: self.bus.is_connected)

    def read_calibration(self):
        out, errors = {}, {}
        for name in self.bus.motors:
            try:
                out[name] = {
                    "id": CAL[name]["id"], "drive_mode": 0,
                    "homing_offset": self.bus.read("Homing_Offset", name, normalize=False),
                    "range_min": self.bus.read("Min_Position_Limit", name, normalize=False),
                    "range_max": self.bus.read("Max_Position_Limit", name, normalize=False),
                }
            except Exception as exc:  # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"
        return out, errors

    def set_torque(self, on):
        self.bus.enable_torque() if on else self.bus.disable_torque()

    def read_positions(self, normalize=False):
        return self.bus.sync_read("Present_Position", normalize=normalize)

    def disconnect(self):
        self.bus.disconnect()


@pytest.fixture
def core():
    """A ConsoleCore with fake arms and the worker thread not started."""
    c = ConsoleCore()
    for role in ("follower", "leader"):
        c._sessions[role] = FakeSession(role, FakeBus())  # noqa: SLF001
    return c


# --------------------------------------------------------------------------- calibration cache


def test_ensure_calibration_loads_from_motors(core):
    core._ensure_calibration("leader")  # noqa: SLF001
    bus = core._sessions["leader"].bus  # noqa: SLF001
    assert set(bus.calibration) == set(bus.motors)
    assert all(isinstance(v, MotorCalibration) for v in bus.calibration.values())
    assert bus.calibration["elbow_flex"].range_max == 3200


def test_ensure_calibration_is_a_noop_when_already_loaded(core):
    """A second call must not re-read — each read talks to six motors."""
    session = core._sessions["leader"]  # noqa: SLF001
    original = session.read_calibration
    calls: list[int] = []

    def counting():
        calls.append(1)
        return original()

    session.read_calibration = counting

    core._ensure_calibration("leader")  # noqa: SLF001
    assert len(calls) == 1
    core._ensure_calibration("leader")  # noqa: SLF001
    assert len(calls) == 1, "must not re-read a complete cache"


def test_ensure_calibration_raises_a_readable_error_when_a_motor_is_dead(core):
    """An incomplete read must produce an actionable message naming the joint."""
    core._sessions["leader"] = FakeSession("leader", FakeBus(fail_reads={"gripper"}))  # noqa: SLF001
    with pytest.raises(RuntimeError) as ei:
        core._ensure_calibration("leader")  # noqa: SLF001
    msg = str(ei.value)
    assert "gripper" in msg
    assert "Device tab" in msg, "must tell the user where to fix it"
    assert "has no calibration registered" not in msg, "must not leak upstream's raw message"


def test_ensure_calibration_non_strict_only_logs(core):
    core._sessions["leader"] = FakeSession("leader", FakeBus(fail_reads={"wrist_roll"}))  # noqa: SLF001
    core._ensure_calibration("leader", strict=False)  # noqa: SLF001
    assert core.snapshot()["arms"]["leader"]["error"] is not None


# --------------------------------------------------------------------------- teleop preconditions


def test_teleop_recovers_from_a_wiped_calibration_cache(core):
    """Reproduces the production failure: a wiped cache must self-heal, not raise.

    ``set_half_turn_homings()`` calls ``reset_calibration()`` internally, which
    empties the cache (``motors_bus.py:791`` -> ``:773``). Starting teleop after
    that used to raise ``has no calibration registered``.
    """
    leader_bus = core._sessions["leader"].bus  # noqa: SLF001
    core._ensure_calibration("leader")          # noqa: SLF001
    leader_bus.calibration = {}                 # as set_half_turn_homings would leave it

    core._act_teleop_start()                    # noqa: SLF001 - must not raise
    assert set(leader_bus.calibration) == set(leader_bus.motors)
    assert core.snapshot()["teleop"]["running"] is True


def test_teleop_refuses_while_calibration_is_mid_flight(core):
    """Mid-calibration (homing written, range not yet) must not start teleop."""
    core._ensure_calibration("leader")  # noqa: SLF001
    core._ensure_calibration("follower")  # noqa: SLF001
    core._state["calib"]["phase"] = "homed"  # noqa: SLF001

    with pytest.raises(RuntimeError) as ei:
        core._act_teleop_start()  # noqa: SLF001
    assert "Calibration is still open" in str(ei.value)
    assert core.snapshot()["teleop"]["running"] is False


def test_teleop_refuses_when_a_motor_is_missing(core):
    core._sessions["follower"] = FakeSession("follower", FakeBus(fail_reads={"gripper"}))  # noqa: SLF001
    with pytest.raises(RuntimeError) as ei:
        core._act_teleop_start()  # noqa: SLF001
    assert "gripper" in str(ei.value)


def test_teleop_zeros_the_goal_before_enabling_torque(core):
    """The goal must be set to the present position first, or the arm jumps on enable."""
    core._ensure_calibration("follower")  # noqa: SLF001
    core._ensure_calibration("leader")    # noqa: SLF001
    core._act_teleop_start()              # noqa: SLF001

    follower_bus = core._sessions["follower"].bus  # noqa: SLF001
    assert follower_bus.goal_writes, "Goal_Position must be written before torque is enabled"
    assert follower_bus.torque is True
    assert core._sessions["leader"].bus.torque is False, "the leader must stay back-drivable"  # noqa: SLF001


def test_estop_releases_torque_and_stops(core):
    core._ensure_calibration("follower")  # noqa: SLF001
    core._ensure_calibration("leader")    # noqa: SLF001
    core._act_teleop_start()              # noqa: SLF001

    core._act_release_all()               # noqa: SLF001
    assert core._sessions["follower"].bus.torque is False  # noqa: SLF001
    assert core._sessions["leader"].bus.torque is False    # noqa: SLF001
    assert core.snapshot()["teleop"]["running"] is False


def test_teleop_watchdog_stops_after_repeated_failures(core):
    """A yanked cable must stop the loop instead of spinning on errors forever."""
    core._ensure_calibration("follower")  # noqa: SLF001
    core._ensure_calibration("leader")    # noqa: SLF001
    core._act_teleop_start()              # noqa: SLF001

    def boom(*a, **kw):
        raise ConnectionError("bus went away")

    core._sessions["leader"].read_positions = boom  # noqa: SLF001
    for _ in range(worker_mod.TELEOP_MAX_FAILS):
        core._teleop_step()  # noqa: SLF001

    assert core.snapshot()["teleop"]["running"] is False


def test_shutdown_is_idempotent_and_drops_sessions():
    c = ConsoleCore()
    c._sessions["leader"] = FakeSession("leader", FakeBus())  # noqa: SLF001
    c.shutdown()
    assert c._sessions == {}          # noqa: SLF001
    c.shutdown()                      # a second call must not blow up


def test_shutdown_releases_torque():
    """Leaving an arm energised after the app exits would be dangerous."""
    c = ConsoleCore()
    bus = FakeBus()
    bus.enable_torque()
    c._sessions["follower"] = FakeSession("follower", bus)  # noqa: SLF001
    c.shutdown()
    assert bus.torque is False


# --------------------------------------------------------------------------- dispatch


def test_submit_rejects_unknown_actions():
    c = ConsoleCore()
    assert c.submit("no_such_action")["ok"] is False


def test_submit_accepts_known_actions():
    c = ConsoleCore()
    assert c.submit("refresh_ports")["ok"] is True


def test_log_is_a_bounded_ring_buffer():
    c = ConsoleCore()
    for i in range(worker_mod.LOG_LIMIT + 50):
        c._log("info", f"line {i}")  # noqa: SLF001
    log = c.snapshot()["log"]
    assert len(log) == worker_mod.LOG_LIMIT
    assert log[-1]["msg"] == f"line {worker_mod.LOG_LIMIT + 49}"


def test_snapshot_is_json_serializable(core):
    core._ensure_calibration("leader")  # noqa: SLF001
    json.dumps(core.snapshot(), default=str)
