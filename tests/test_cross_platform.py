"""Guards for Windows/Linux parity.

The console is meant to work out of the box on both, so anything that branches on
the platform gets a test here. The risks are well known:

* ``cv2.CAP_DSHOW`` is Windows-only; passing it on Linux fails to open anything.
* Windows and macOS address cameras by integer index, Linux by ``/dev/video*``.
* ``msvcrt`` only exists on Windows and must not be imported at module scope.
* Serial port names differ (``COM24`` vs ``/dev/ttyUSB0``), which is why the UI
  fills a dropdown from enumeration rather than asking for free text.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

from app import cameras, paths
from app.hardware import SO101_LAYOUT, build_motors


# --------------------------------------------------------------------------- cameras


def test_scan_targets_on_linux_uses_dev_nodes(monkeypatch):
    """Linux must glob /dev/video* — probing integer indices there is slow and wrong."""
    monkeypatch.setattr(cameras.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cameras.glob, "glob", lambda pattern: ["/dev/video0", "/dev/video2"])
    assert cameras._scan_targets() == ["/dev/video0", "/dev/video2"]


def test_scan_targets_on_windows_probes_the_full_index_range(monkeypatch):
    """A camera can enumerate at any index, so the whole range gets probed."""
    monkeypatch.setattr(cameras.platform, "system", lambda: "Windows")
    targets = cameras._scan_targets()
    assert targets == list(range(cameras.MAX_CAMERA_INDEX))
    assert targets[0] == 0 and len(targets) == 60


def test_scan_targets_on_macos_probes_the_full_index_range(monkeypatch):
    monkeypatch.setattr(cameras.platform, "system", lambda: "Darwin")
    assert cameras._scan_targets() == list(range(cameras.MAX_CAMERA_INDEX))


def test_capture_backend_is_valid_on_this_platform():
    """CAP_DSHOW on Windows, something portable elsewhere. Never a Linux-invalid flag."""
    if platform.system() == "Windows":
        assert cameras.CAP_BACKEND == cameras.cv2.CAP_DSHOW
    else:
        assert cameras.CAP_BACKEND == cameras.cv2.CAP_ANY
        assert cameras.CAP_BACKEND != cameras.cv2.CAP_DSHOW


def test_camera_index_may_be_a_path():
    """CameraSlot must accept both ``0`` and ``"/dev/video0"``."""
    slot = cameras.CameraSlot(name="handeye", index="/dev/video0")
    assert slot.index == "/dev/video0"
    assert slot.name == "handeye"


def test_numeric_string_index_becomes_an_int():
    """Regression: the browser sends free text, so Windows indices arrive as "0".

    OpenCV reads a str as a filename, so ``VideoCapture("0")`` fails with
    "backend is generally available but can't be used to capture by name".
    """
    slot = cameras.CameraSlot(name="handeye", index="0")
    assert slot.index == 0
    assert isinstance(slot.index, int)


def test_coerce_index_covers_every_input_shape():
    assert cameras.coerce_index(0) == 0
    assert cameras.coerce_index("0") == 0
    assert cameras.coerce_index("  12  ") == 12
    assert cameras.coerce_index("/dev/video0") == "/dev/video0"
    assert cameras.coerce_index("/dev/video10") == "/dev/video10"
    assert cameras.coerce_index("rtsp://cam/stream") == "rtsp://cam/stream"
    # A path of digits with a prefix must not be mangled into a number.
    assert cameras.coerce_index("cam0") == "cam0"


# --------------------------------------------------------------------------- imports


def test_msvcrt_is_not_imported_at_module_scope():
    """``msvcrt`` exists only on Windows; importing it at module level breaks Linux.

    It is used inside ``enter_pressed``, guarded by ``sys.platform``.
    """
    src = (Path(__file__).parent.parent / "vendor" / "lerobot_motors" / "_compat.py").read_text(
        encoding="utf-8"
    )
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import msvcrt", "from msvcrt")):
            assert line.startswith(" "), "msvcrt must be imported lazily, inside the function"
    assert "msvcrt" not in sys.modules or platform.system() == "Windows"


# --------------------------------------------------------------------------- paths


def test_calibration_path_uses_pathlib_not_strings():
    """Path building must be separator-agnostic so it works on both platforms."""
    p = paths.calibration_path("follower", "arm1")
    assert isinstance(p, Path)
    assert p.name == "arm1.json"
    assert p.parent.name == "so_follower"
    assert p.parent.parent.name == "robots"


def test_leader_and_follower_live_in_different_trees():
    """robots/ vs teleoperators/ — matches upstream and must not be swapped."""
    assert paths.calibration_path("follower", "x").parts[-3] == "robots"
    assert paths.calibration_path("leader", "x").parts[-3] == "teleoperators"


def test_env_override_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_LEROBOT_CALIBRATION", str(tmp_path))
    assert paths.calibration_root() == tmp_path
    assert str(paths.calibration_path("leader", "a")).startswith(str(tmp_path))


# --------------------------------------------------------------------------- hardware


def test_serial_port_enumeration_works_on_both_platforms():
    """pyserial abstracts COM vs /dev/tty; the UI relies on this returning a list."""
    from app.hardware import list_serial_ports

    ports = list_serial_ports()
    assert isinstance(ports, list)
    for p in ports:
        assert isinstance(p["device"], str) and p["device"]


@pytest.mark.parametrize("port", ["COM24", "/dev/ttyUSB0", "/dev/ttyACM0"])
def test_port_names_are_passed_through_verbatim(port):
    """A port name is opaque — no normalisation that could mangle a POSIX path."""
    from vendor.lerobot_motors.feetech import FeetechMotorsBus

    bus = FeetechMotorsBus(port=port, motors=build_motors())
    assert bus.port == port
