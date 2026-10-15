"""Live streaming to the Rerun viewer.

This is the equivalent of ``lerobot-teleoperate --display_data=true``: joint
values as time series and camera feeds as images, in the Rerun desktop viewer.
The console cannot simply shell out to that command — it holds the serial ports
itself — so it feeds the same kind of data from its own teleop loop.

Two details that matter:

* The viewer binary ships inside the ``rerun-sdk`` wheel, next to the interpreter
  (``.venv/Scripts/rerun.exe`` on Windows, ``.venv/bin/rerun`` on Linux). The SDK
  otherwise searches ``PATH``, which fails unless the venv is activated — so the
  path is passed explicitly.
* ``spawn()`` blocks until the viewer accepts connections, which takes seconds.
  It runs on its own thread so the teleop loop is never stalled by it.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: Root entity paths, so the blueprint can group them.
JOINTS_ROOT = "joints"
CAMERAS_ROOT = "camera"


def viewer_executable() -> Path | None:
    """Locate the Rerun viewer that ships with ``rerun-sdk``.

    Returns:
        The path to the binary, or ``None`` when it is not next to the interpreter.
    """
    name = "rerun.exe" if sys.platform == "win32" else "rerun"
    candidate = Path(sys.executable).parent / name
    return candidate if candidate.is_file() else None


def _build_blueprint(rr, rrb, joints: list[str]):
    """Cameras on the left, joint time series on the right."""
    return rrb.Blueprint(
        rrb.Horizontal(
            rrb.Spatial2DView(origin=CAMERAS_ROOT, name="cameras"),
            rrb.TimeSeriesView(origin=JOINTS_ROOT, name="joints"),
        ),
        collapse_panels=True,
    )


class RerunSink:
    """Streams joint values and camera frames to a Rerun viewer.

    All methods are safe to call from any thread; ``log()`` is a no-op until the
    viewer is up, so the teleop loop never waits on it.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = False
        self._error: str | None = None
        self._rr = None
        self._logged_frames: dict[str, float] = {}
        self._joint_names: list[str] = []

    # ------------------------------------------------------------------ lifecycle

    @property
    def status(self) -> dict:
        """Snapshot for the UI."""
        return {
            "enabled": self._thread is not None,
            "ready": self._ready,
            "error": self._error,
        }

    def start(self, joint_names: list[str] | None = None) -> None:
        """Spawn the viewer and begin streaming.

        Args:
            joint_names: Joints to plot, in order. Required for the blueprint.
        """
        if self._thread is not None:
            return
        self._joint_names = list(joint_names or [])
        self._error = None
        self._ready = False
        self._thread = threading.Thread(target=self._run, name="rerun-sink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop streaming and disconnect the viewer."""
        thread, self._thread = self._thread, None
        self._ready = False
        self._logged_frames.clear()
        if thread is not None:
            thread.join(timeout=5.0)
        rr = self._rr
        if rr is not None:
            for name in ("rerun_shutdown", "disconnect"):
                fn = getattr(rr, name, None)
                if fn is not None:
                    try:
                        fn()
                        break
                    except Exception:  # noqa: BLE001 - shutdown is best-effort
                        continue

    def _run(self) -> None:
        try:
            import rerun as rr
        except ImportError as exc:
            self._error = f"rerun-sdk is not installed: {exc}"
            logger.warning(self._error)
            return

        try:
            rr.init("so101_debug_console")
            exe = viewer_executable()
            if exe is None:
                self._error = (
                    "Rerun viewer binary not found next to the interpreter. "
                    "Reinstall with: uv pip install rerun-sdk"
                )
                logger.warning(self._error)
                return
            # Blocks until the viewer is listening, which is why it runs here.
            rr.spawn(executable_path=str(exe), hide_welcome_screen=True)
            self._rr = rr
            try:
                import rerun.blueprint as rrb

                rr.send_blueprint(_build_blueprint(rr, rrb, self._joint_names))
            except Exception:  # noqa: BLE001 - a layout failure must not stop the stream
                logger.warning("could not send the rerun blueprint", exc_info=True)
            self._ready = True
            logger.info("rerun viewer started: %s", exe)
        except Exception as exc:  # noqa: BLE001 - report to the UI instead of dying
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("failed to start the rerun viewer: %s", exc)

    # ------------------------------------------------------------------ logging

    def log(self, joints: dict, cameras: dict[str, tuple[bytes, float]] | None = None) -> None:
        """Log one frame of data.

        Args:
            joints: ``{joint_name: (leader_value, follower_value)}``. Either side
                may be ``None``.
            cameras: ``{camera_name: (jpeg_bytes, frame_stamp)}``. Frames are only
                logged when the stamp changes, so a stalled camera does not
                re-send identical images.
        """
        if not self._ready or self._rr is None:
            return
        rr = self._rr
        with self._lock:
            try:
                for joint, (leader, follower) in joints.items():
                    if leader is not None:
                        rr.log(f"{JOINTS_ROOT}/{joint}/leader", rr.Scalars(float(leader)))
                    if follower is not None:
                        rr.log(f"{JOINTS_ROOT}/{joint}/follower", rr.Scalars(float(follower)))

                for name, (jpeg, stamp) in (cameras or {}).items():
                    if self._logged_frames.get(name) == stamp:
                        continue
                    self._logged_frames[name] = stamp
                    rr.log(
                        f"{CAMERAS_ROOT}/{name}",
                        # Already JPEG from the MJPEG pipeline, so no re-encoding.
                        rr.EncodedImage(contents=jpeg, media_type="image/jpeg"),
                    )
            except Exception as exc:  # noqa: BLE001 - a dropped frame must not stop teleop
                logger.debug("rerun log failed: %s", exc)


__all__ = ["CAMERAS_ROOT", "JOINTS_ROOT", "RerunSink", "viewer_executable"]
