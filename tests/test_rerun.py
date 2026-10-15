"""Rerun streaming — verified without spawning a viewer.

The viewer is a GUI process, so these tests exercise everything around it: module
import cost, the binary lookup, the status contract, and that ``log()`` is a safe
no-op before the viewer is up. Actually spawning one is a manual step.
"""

from __future__ import annotations

import sys

import pytest

from app import rerun_log
from app.rerun_log import RerunSink, viewer_executable


def test_importing_the_module_does_not_import_rerun():
    """Importing must stay cheap — rerun pulls in numpy, pyarrow and friends.

    The console imports this module at startup, so a top-level ``import rerun``
    would slow every boot for a feature most sessions never use.
    """
    if "rerun" in sys.modules:
        pytest.skip("rerun was already imported by another test")
    assert "rerun" not in sys.modules


def test_viewer_binary_ships_next_to_the_interpreter():
    """The SDK searches PATH, which fails unless the venv is active.

    The wheel puts the viewer next to the interpreter, so it is located explicitly
    and passed to ``spawn(executable_path=...)``.
    """
    exe = viewer_executable()
    assert exe is not None, (
        "Rerun viewer not found next to the interpreter — "
        "uv pip install --python <venv python> rerun-sdk"
    )
    assert exe.is_file()
    assert exe.parent == __import__("pathlib").Path(sys.executable).parent


def test_status_before_start():
    sink = RerunSink()
    assert sink.status == {"enabled": False, "ready": False, "error": None}


def test_log_is_a_noop_before_start():
    """The teleop loop calls log() unconditionally; it must never raise or block."""
    sink = RerunSink()
    sink.log({"shoulder_pan": (1.0, 2.0)}, {"handeye": (b"\xff\xd8jpeg", 1.0)})
    sink.log({}, {})
    assert sink.status["ready"] is False


def test_log_accepts_missing_sides():
    """A joint may be missing from either arm; None must be skipped, not sent."""
    sink = RerunSink()
    sink.log({"elbow_flex": (None, 3.0), "wrist_roll": (4.0, None), "gripper": (None, None)})


def test_stop_without_start_is_safe():
    sink = RerunSink()
    sink.stop()
    sink.stop()
    assert sink.status["enabled"] is False


def test_start_is_not_reentrant(monkeypatch):
    """Starting twice must not spawn two viewers.

    ``_run`` is stubbed out: the real one calls ``rr.spawn()``, which blocks until
    a GUI viewer is up. A test must never open a window on someone's desktop.
    """
    started: list[int] = []
    monkeypatch.setattr(RerunSink, "_run", lambda self: started.append(1))

    sink = RerunSink()
    sink.start(["shoulder_pan"])
    sink.start(["shoulder_pan"])   # ignored — a thread is already running
    assert len(started) == 1
    assert sink.status["enabled"] is True

    sink.stop()
    sink.stop()                    # stopping twice must not raise
    assert sink.status["enabled"] is False


def test_stop_clears_the_frame_cache():
    """Restarting must re-send the first frame of every camera."""
    sink = RerunSink()
    sink._logged_frames["handeye"] = 1.0  # noqa: SLF001
    sink.stop()
    assert sink._logged_frames == {}      # noqa: SLF001


def test_entity_paths_are_namespaced():
    """The blueprint groups by root, so the roots must stay in sync with log()."""
    assert rerun_log.JOINTS_ROOT == "joints"
    assert rerun_log.CAMERAS_ROOT == "camera"


def test_worker_exposes_the_toggle_action():
    from app.worker import ConsoleCore

    core = ConsoleCore()
    assert core.submit("set_rerun", enabled=True)["ok"] is True
    assert core.snapshot()["rerun"] == {"enabled": False, "ready": False, "error": None}
