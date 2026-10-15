"""Hardware wrapper — the parts verifiable without a real arm.

The focus is **dependency completeness**. The vendored motor layer unconditionally
requires several packages when a bus is constructed (``pyserial`` / ``deepdiff`` /
``feetech-servo-sdk``). On 2026-10-15 ``deepdiff`` was missing, which made port
probing and connecting entirely unusable — and the smoke test of the day did not
catch it, because it only asserted "an error occurred" and an ``ImportError``
satisfies that.

These tests close that hole: they assert the **reason** for a failure, not merely
that one happened.
"""

from __future__ import annotations

import pytest

from app.hardware import (
    EXPECTED_MODELS,
    SO101_LAYOUT,
    ArmSession,
    build_calibration,
    build_motors,
    list_serial_ports,
    probe_port,
)
from vendor.lerobot_motors import MotorNormMode
from vendor.lerobot_motors.feetech import FeetechMotorsBus

FAKE_PORT = "COM_DOES_NOT_EXIST"


def test_bus_can_be_constructed_without_hardware():
    """Constructing a bus must not fail on a missing dependency.

    This is the direct regression for the deepdiff incident. The constructor only
    checks dependencies and initialises fields; it does not open the port, so a
    fake port name is safe.
    """
    bus = FeetechMotorsBus(port=FAKE_PORT, motors=build_motors())
    assert len(bus.motors) == 6
    assert not bus.is_connected
    assert bus.port == FAKE_PORT


def test_every_required_package_of_the_vendored_layer_is_importable():
    """Whatever the motor layer demands at construction time must be installed."""
    from vendor.lerobot_motors._compat import (
        _deepdiff_available,
        _feetech_sdk_available,
        _serial_available,
    )

    missing = [
        name
        for name, available in (
            ("pyserial", _serial_available),
            ("deepdiff", _deepdiff_available),
            ("feetech-servo-sdk", _feetech_sdk_available),
        )
        if not available
    ]
    assert not missing, (
        f"missing dependencies {missing} — install them with:"
        "\n    uv pip install --python <this venv's python> " + " ".join(missing)
    )


def test_probe_failure_is_about_the_port_not_a_missing_module():
    """Probing a non-existent port must fail at the serial layer, not on an import.

    This is precisely the misjudgement that slipped through before: asserting "there
    was an error" is too loose; the assertion has to be about **which** error.
    """
    res = probe_port(FAKE_PORT, timeout_ms=100)

    assert res.ok is False
    assert res.error, "a failure must explain itself"
    for wrong in ("ImportError", "ModuleNotFoundError", "No module named"):
        assert wrong not in res.error, f"this is a missing dependency, not a port problem: {res.error}"


def test_probe_failure_explains_itself_instead_of_leaking_upstream_text():
    """A real case: COM1 is a motherboard UART and cannot run at 1 Mbaud.

    Upstream's message is "Could not connect on port 'COM1'. ... Try running
    `lerobot-find-port`" — raw, and a CLI instruction that means nothing in a web
    UI. The probe must say what actually went wrong.
    """
    res = probe_port("COM1", timeout_ms=100)
    if res.ok:  # no COM1 on this machine
        pytest.skip("no COM1 present")

    assert res.error
    assert "lerobot-find-port" not in res.error, "must not leak the upstream CLI hint"
    assert "Traceback" not in res.error
    # It should name the port and point at a cause.
    assert "COM1" in res.error


def test_open_failure_explanation_distinguishes_the_two_cases():
    """Opens-at-low-speed vs cannot-open-at-all are different problems."""
    from app.hardware import explain_open_failure

    missing = explain_open_failure("COM_DEFINITELY_NOT_A_PORT")
    assert "could not be opened" in missing
    assert "baudrate" not in missing, "a missing port is not a baudrate problem"


def test_probe_pings_each_motor_individually(monkeypatch):
    """Enumeration must ping motor by motor, never use ``broadcast_ping``.

    Measured on 2026-10-15 with both arms powered and online::

        broadcast_ping  ->  0-1 motors, 1576 ms
        per-motor ping  ->  6 motors,      5 ms

    A broadcast asks every motor to answer simultaneously, so the replies collide
    on the bus and the adapter barely catches any; and each silent motor costs a
    full timeout on top. Upstream ``_assert_motors_exist`` pings individually too.
    """
    calls: list[tuple] = []

    class FakeBus:
        def __init__(self, port, motors):
            self.port, self.motors = port, motors

        def set_timeout(self, ms):
            calls.append(("set_timeout", ms))

        def connect(self, handshake=True):
            calls.append(("connect", handshake))

        def broadcast_ping(self, **kw):
            calls.append(("broadcast_ping",))
            return None

        def ping(self, name, **kw):
            calls.append(("ping", name))
            return 777

        def disconnect(self, disable_torque=True):
            calls.append(("disconnect",))

    monkeypatch.setattr("app.hardware.FeetechMotorsBus", FakeBus)
    res = probe_port(FAKE_PORT)

    assert not [c for c in calls if c[0] == "broadcast_ping"], "regressed to broadcast_ping"
    assert len([c for c in calls if c[0] == "ping"]) == 6, "all six joints must be pinged"
    assert ("connect", False) in calls, "must skip the handshake, or a missing motor raises"
    assert res.motors == {i: 777 for i in range(1, 7)}


def test_arm_session_connect_also_pings_individually(monkeypatch):
    """The second instance of the same trap.

    After fixing `probe_port`, `ArmSession.connect()` was missed, so the Device tab
    reported 1/6 motors — while the calibration values read back from the same
    motors were complete and matching, proving the serial link itself was fine.
    This test pins both call sites to per-motor pings.
    """
    calls: list[tuple] = []

    class FakeBus:
        def __init__(self, port, motors):
            self.port, self.motors = port, motors

        def connect(self, handshake=True):
            calls.append(("connect", handshake))

        def broadcast_ping(self, **kw):
            calls.append(("broadcast_ping",))
            return {1: 777}

        def ping(self, name, **kw):
            calls.append(("ping", name))
            return 777

    monkeypatch.setattr("app.hardware.FeetechMotorsBus", FakeBus)
    session = ArmSession(role="follower", port=FAKE_PORT, arm_id="x")
    found = session.connect()

    assert not [c for c in calls if c[0] == "broadcast_ping"], "regressed to broadcast_ping"
    assert len([c for c in calls if c[0] == "ping"]) == 6
    assert ("connect", False) in calls
    assert found == {i: 777 for i in range(1, 7)}


def test_probe_reports_a_partial_bus(monkeypatch):
    """A partial bus must report exactly who is missing — the gripper dropout case."""

    class FakeBus:
        def __init__(self, port, motors):
            self.motors = motors

        def set_timeout(self, ms):
            pass

        def connect(self, handshake=True):
            pass

        def ping(self, name, **kw):
            return None if name == "gripper" else 777

        def disconnect(self, disable_torque=True):
            pass

    monkeypatch.setattr("app.hardware.FeetechMotorsBus", FakeBus)
    res = probe_port(FAKE_PORT)

    assert res.ok is True
    assert sorted(res.motors) == [1, 2, 3, 4, 5]
    assert 6 not in res.motors


def test_serial_port_enumeration_returns_well_formed_rows():
    ports = list_serial_ports()
    assert isinstance(ports, list)
    for p in ports:
        assert {"device", "kind", "description", "hwid", "serial_number"} <= set(p)
        assert p["device"]


def test_localized_descriptions_are_dropped():
    """Windows reports port descriptions in the system locale.

    A Chinese Windows returns ``通信端口 (COM1)``, which must not reach an
    English-only interface. Anything non-ASCII is dropped at the source.
    """
    from app.hardware import ascii_only

    assert ascii_only("USB-Enhanced-SERIAL CH343 (COM24)") == "USB-Enhanced-SERIAL CH343 (COM24)"
    assert ascii_only("通信端口 (COM1)") == ""
    assert ascii_only("") == ""
    assert ascii_only(None) == ""


def test_every_enumerated_description_is_ascii():
    """Whatever the OS hands back, the UI never sees a non-ASCII description."""
    for p in list_serial_ports():
        p["description"].encode("ascii")  # raises if a localized string slipped through


def test_builtin_ports_get_an_english_name():
    """A Chinese Windows describes COM1 as "通信端口 (COM1)".

    That localized string must not reach the UI, but leaving the column blank is
    unhelpful too. The PNP id is locale-independent, so the English name is derived
    from it.
    """
    from app.hardware import port_description

    assert port_description(r"ACPI\PNP0501\0", "通信端口 (COM1)") == "Communications Port (16550 UART)"
    assert port_description(r"ACPI\PNP0500\0", "通信端口 (COM1)") == "Communications Port (standard UART)"
    # Bare PNP id, as some systems report it.
    assert port_description("PNP0501", None) == "Communications Port (16550 UART)"


def test_usb_descriptions_pass_through_untouched():
    from app.hardware import port_description

    assert (port_description(r"USB\VID_1A86&PID_55D3\5C4C128534",
                             "USB-Enhanced-SERIAL CH343 (COM24)")
            == "USB-Enhanced-SERIAL CH343 (COM24)")


def test_unknown_localized_description_yields_empty_not_guesswork():
    """Better an empty cell than an invented name — Kind and HWID still identify it."""
    from app.hardware import port_description

    assert port_description(r"ACPI\PNP9999\0", "某个本地化名称") == ""
    assert port_description("", None) == ""


def test_pnp_id_extraction():
    from app.hardware import pnp_id

    assert pnp_id(r"ACPI\PNP0501\0") == "PNP0501"
    assert pnp_id(r"PNP0501\0") == "PNP0501"
    assert pnp_id(r"USB\VID_1A86&PID_55D3\X") == ""
    assert pnp_id("") == ""


def test_port_kind_is_locale_independent():
    """Kind comes from the hardware id, so it reads the same on any Windows."""
    from app.hardware import port_kind

    assert port_kind(r"USB\VID_1A86&PID_55D3\5C4C128534") == "USB adapter"
    assert port_kind(r"ACPI\PNP0501\0") == "Built-in port"
    assert port_kind(r"PNP0501\0") == "Built-in port"
    assert port_kind("") == "Serial port"


def test_builtin_and_usb_ports_are_distinguishable():
    """A built-in port can never be an arm — it cannot reach 1 Mbaud. The kind
    column is what tells the user that before a probe fails."""
    from app.hardware import port_kind

    kinds = {port_kind(p["hwid"]) for p in list_serial_ports()}
    assert kinds <= {"USB adapter", "Built-in port", "Serial port"}


def test_so101_layout_matches_the_upstream_bus_definition():
    """Joint names and ids must match upstream's bus, or calibration files will not line up."""
    assert SO101_LAYOUT == {
        "shoulder_pan": (1, MotorNormMode.DEGREES),
        "shoulder_lift": (2, MotorNormMode.DEGREES),
        "elbow_flex": (3, MotorNormMode.DEGREES),
        "wrist_flex": (4, MotorNormMode.DEGREES),
        "wrist_roll": (5, MotorNormMode.DEGREES),
        "gripper": (6, MotorNormMode.RANGE_0_100),
    }
    assert EXPECTED_MODELS == {i: 777 for i in range(1, 7)}


def test_build_calibration_produces_the_upstream_json_shape():
    """The result must feed straight into paths.save_calibration_json without losing a field."""
    mins = {name: 800 for name in SO101_LAYOUT}
    maxes = {name: 3200 for name in SO101_LAYOUT}
    offsets = {name: -100 for name in SO101_LAYOUT}

    cal = build_calibration(mins, maxes, offsets)

    assert set(cal) == set(SO101_LAYOUT)
    for name, entry in cal.items():
        assert set(entry) == {"id", "drive_mode", "homing_offset", "range_min", "range_max"}
        assert entry["id"] == SO101_LAYOUT[name][0]
        assert entry["drive_mode"] == 0
        assert entry["homing_offset"] == -100
        assert entry["range_min"] == 800
        assert entry["range_max"] == 3200


@pytest.mark.parametrize("role", ["bogus", "", "Follower"])
def test_arm_session_rejects_an_unknown_role(role):
    with pytest.raises(ValueError):
        ArmSession(role=role, port=FAKE_PORT, arm_id="x")
