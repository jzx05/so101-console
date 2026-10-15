"""Background worker thread — owns the serial ports; the UI only queues commands
and reads state snapshots.

Why single-threaded: ``pyserial`` port handles are not reentrant, and FastAPI
callbacks run on arbitrary worker threads. If a request thread touched the bus
directly, two concurrent requests could interleave the packets of one sync_read.
So every hardware access funnels through this one thread.

Two deliberate exceptions, both thread-safe and neither touching the serial bus:

* **E-stop** goes through a :class:`threading.Event` set directly by the request
  thread, bypassing the command queue. Otherwise it would sit behind a slow bus
  call — and with a motor missing, one ``sync_read`` waits out three timeouts.
* **Camera frames** come from each camera's own reader thread; they share no
  state with the bus.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Any, Callable

from vendor.lerobot_motors import MotorCalibration

from . import cameras as cameras_mod
from . import diagnostics, paths
from .calibration import SweepRecorder
from .hardware import (
    EXPECTED_MODELS,
    SO101_LAYOUT,
    ArmSession,
    build_calibration,
    list_serial_ports,
    probe_port,
)
from .rerun_log import RerunSink

logger = logging.getLogger(__name__)

#: Consecutive teleop failures before the loop stops itself. Watchdog for a yanked cable.
TELEOP_MAX_FAILS = 5

#: Log ring buffer size.
LOG_LIMIT = 300

#: Calibration phases during which teleop must not start.
_CALIB_IN_PROGRESS = ("prepared", "homed", "sweeping", "review")


def _finding(f) -> dict:
    """Convert a diagnostics Finding into a JSON-safe dict."""
    return asdict(f)


class ConsoleCore:
    """Backend core. Every method here is safe to call from any thread."""

    def __init__(self) -> None:
        self._cmd_q: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._stop = threading.Event()
        self._estop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = self._blank_state()

        self._sessions: dict[str, ArmSession] = {}
        self._sweep: dict[str, SweepRecorder | None] = {"follower": None, "leader": None}
        self._draft: dict[str, dict[str, dict] | None] = {"follower": None, "leader": None}
        self._cameras = cameras_mod.CameraManager()
        self._rerun = RerunSink()
        self._teleop_running = False
        self._teleop_fails = 0
        self._teleop_release_on_stop = True
        self._teleop_rate: deque[float] = deque(maxlen=30)

    # ------------------------------------------------------------------ state

    @staticmethod
    def _blank_state() -> dict[str, Any]:
        arm = {
            "connected": False,
            "port": "",
            "id": "",
            "motors": {},
            "expected": EXPECTED_MODELS,
            "findings": [],
            "json_path": "",
            "json_exists": False,
            "torque": None,
            "error": None,
        }
        return {
            "seq": 0,
            "ts": time.time(),
            "busy": None,
            "ports": [],
            "probes": {},
            "arms": {"follower": dict(arm), "leader": dict(arm)},
            "pair": [],
            "calib": {
                "role": None,
                "phase": "idle",
                "sweep": {},
                "draft": {},
                "findings": [],
                "flat_joints": [],
                "applied": False,
                "saved_path": None,
            },
            "teleop": {"running": False, "hz": 0.0, "iter": 0, "rows": [], "fails": 0, "error": None},
            "rerun": {"enabled": False, "ready": False, "error": None},
            "cameras": {},
            "scan": {"cameras": None, "scanning": False},
            "log": [],
            "last_error": None,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the current state. Callers must treat it as read-only."""
        with self._state_lock:
            return self._state

    def _log(self, level: str, msg: str) -> None:
        with self._state_lock:
            log = self._state["log"]
            log.append({"ts": time.time(), "level": level, "msg": msg})
            del log[:-LOG_LIMIT]

    def _set_error(self, msg: str | None) -> None:
        with self._state_lock:
            self._state["last_error"] = msg

    def _update_arm(self, role: str, **fields) -> None:
        with self._state_lock:
            self._state["arms"][role].update(fields)
            self._state["seq"] += 1

    def _state_phase(self) -> str:
        with self._state_lock:
            return self._state["calib"]["phase"]

    def _arm_id(self, role: str) -> str:
        with self._state_lock:
            return self._state["arms"][role]["id"]

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the worker thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="console-core", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop, release torque, close ports, free cameras. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        # The worker may have been stuck in a bus call; sweep up whatever is left.
        for role in list(self._sessions):
            self._force_release(role)
        self._cameras.close_all()

    def estop(self) -> None:
        """Emergency stop. Sets the flag directly, bypassing the command queue."""
        self._estop.set()
        self._log("warn", "E-stop triggered")

    def submit(self, action: str, **payload) -> dict:
        """Queue a command and return immediately."""
        if not hasattr(self, f"_act_{action}"):
            return {"ok": False, "error": f"unknown action {action!r}"}
        self._cmd_q.put((action, payload))
        return {"ok": True, "queued": action}

    # ------------------------------------------------------------------ main loop

    def _run(self) -> None:
        self._log("info", "worker thread started")
        while not self._stop.is_set():
            busy = self._teleop_running or any(self._sweep.values())
            try:
                action, payload = self._cmd_q.get(timeout=0.005 if busy else 0.05)
            except queue.Empty:
                action = payload = None

            if action == "shutdown":
                break

            if action is not None:
                fn: Callable = getattr(self, f"_act_{action}")
                try:
                    fn(**payload)
                    self._set_error(None)
                except Exception as exc:  # noqa: BLE001 - one bad command must not kill the thread
                    logger.exception("action %s failed", action)
                    self._set_error(f"{action}: {type(exc).__name__}: {exc}")
                    self._log("error", f"{action} failed: {exc}")
                with self._state_lock:
                    self._state["busy"] = None

            if self._teleop_running:
                self._teleop_step()
            else:
                self._sweep_step()

        self._cleanup()

    def _cleanup(self) -> None:
        self._log("info", "shutting down…")
        self._teleop_running = False
        self._rerun.stop()
        for role in list(self._sessions):
            self._force_release(role)
        self._cameras.close_all()
        self._log("info", "all devices released, torque off")

    def _force_release(self, role: str) -> None:
        session = self._sessions.pop(role, None)
        if session is None:
            return
        try:
            session.disconnect()
        except Exception:  # noqa: BLE001
            logger.warning("failed to release %s", role, exc_info=True)
        self._update_arm(role, connected=False, torque=None)

    # ------------------------------------------------------------------ commands: devices

    def _act_refresh_ports(self) -> None:
        ports = list_serial_ports()
        with self._state_lock:
            self._state["ports"] = ports
            self._state["seq"] += 1

    def _act_probe_port(self, port: str) -> None:
        with self._state_lock:
            self._state["busy"] = f"probing {port}"
        res = probe_port(port)
        msg = (
            f"{port}: {sorted(res.motors)} ({res.elapsed_ms:.0f} ms)"
            if res.ok
            else f"{port}: probe failed — {res.error}"
        )
        self._log("info" if res.ok else "error", msg)
        with self._state_lock:
            self._state["probes"][port] = {
                "ok": res.ok,
                "motors": res.motors,
                "error": res.error,
                "elapsed_ms": round(res.elapsed_ms, 1),
            }
            self._state["seq"] += 1

    def _ensure_calibration(self, role: str, strict: bool = True) -> None:
        """Make sure the bus cache holds a **complete** calibration table.

        Two paths empty it:

        1. ``set_half_turn_homings()`` calls ``reset_calibration()`` internally,
           which sets ``bus.calibration = {}`` (``motors_bus.py:791`` -> ``:773``).
           So setting the midpoint and then jumping to teleop hits this.
        2. A transient read failure on any single motor — the earlier
           implementation was all-or-nothing, which made one flaky read take the
           whole arm offline.

        Normalized reads check that cache (``motors_bus.py:855``) and raise a
        message that dumps the entire motor table at the user. Checking up front
        lets us say something actionable instead.

        Args:
            role: Which arm.
            strict: Raise when a joint is unreadable; otherwise just record it.

        Raises:
            RuntimeError: ``strict`` and some joints could not be read.
        """
        session = self._require_session(role)
        bus = session.bus
        if bus.calibration and set(bus.calibration) == set(bus.motors):
            return

        motor_cal, errors = session.read_calibration()
        missing = [m for m in bus.motors if m not in motor_cal]
        if missing:
            detail = ", ".join(f"{m} ({errors.get(m, 'no response')})" for m in missing)
            msg = (
                f"{role}: could not read calibration for {detail}. "
                "Check the Device tab first — all 6 motors should be present and readable."
            )
            if strict:
                raise RuntimeError(msg)
            self._log("warn", msg)
            self._update_arm(role, error=msg)
            return

        bus.calibration = {
            name: MotorCalibration(
                id=bus.motors[name].id,
                drive_mode=0,
                homing_offset=int(cal["homing_offset"]),
                range_min=int(cal["range_min"]),
                range_max=int(cal["range_max"]),
            )
            for name, cal in motor_cal.items()
        }
        self._log("info", f"{role}: calibration read from motors")

    def _act_connect_arm(self, role: str, port: str, arm_id: str) -> None:
        self._force_release(role)
        session = ArmSession(role=role, port=port, arm_id=arm_id)
        found = session.connect()

        # Normalize with the calibration that lives in the motors — that is the
        # effective one. The JSON file is only a copy and may be stale.
        self._sessions[role] = session
        self._ensure_calibration(role, strict=False)

        json_cal = paths.load_calibration_json(role, arm_id)
        self._update_arm(
            role,
            connected=True,
            port=port,
            id=arm_id,
            motors={int(k): int(v) for k, v in found.items()},
            json_path=str(paths.calibration_path(role, arm_id)),
            json_exists=json_cal is not None,
            torque=session.torque_enabled(),
            error=None,
        )
        self._log("info", f"{role} connected on {port} ({len(found)}/6 motors)")
        self._refresh_arm_findings(role)

    def _refresh_arm_findings(self, role: str) -> None:
        """Re-run the motor presence check for one arm."""
        session = self._sessions.get(role)
        if session is None:
            return
        with self._state_lock:
            arm = dict(self._state["arms"][role])
        findings = diagnostics.check_motors(EXPECTED_MODELS, arm.get("motors") or None)
        self._update_arm(role, findings=[_finding(f) for f in findings])

    def _act_disconnect_arm(self, role: str) -> None:
        self._stop_teleop(release=True)
        self._force_release(role)
        self._log("info", f"{role} disconnected")

    def _act_health_check(self, role: str) -> None:
        self._refresh_arm_findings(role)
        self._log("info", f"{role}: check complete")

    def _act_compare_arms(self) -> None:
        """Compare the two calibration files. No hardware needed."""
        leader = paths.load_calibration_json("leader", self._arm_id("leader"))
        follower = paths.load_calibration_json("follower", self._arm_id("follower"))
        if leader is None or follower is None:
            raise RuntimeError(
                "Both calibration files must exist to compare. Set each arm's name on the Device tab."
            )
        findings = diagnostics.check_pair(leader, follower)
        with self._state_lock:
            self._state["pair"] = [_finding(f) for f in findings]
            self._state["seq"] += 1
        self._log("info", "leader/follower comparison done")

    def _act_set_arm_id(self, role: str, arm_id: str) -> None:
        """Set an arm's name without connecting, so its file can be read and diagnosed."""
        path = paths.calibration_path(role, arm_id)
        self._update_arm(role, id=arm_id, json_path=str(path), json_exists=path.is_file())

    # ------------------------------------------------------------------ commands: cameras

    def _act_scan_cameras(self) -> None:
        with self._state_lock:
            self._state["scan"] = {"cameras": None, "scanning": True}
            self._state["busy"] = "scanning cameras"
        found = cameras_mod.scan_cameras()
        with self._state_lock:
            self._state["scan"] = {"cameras": found, "scanning": False}
            self._state["seq"] += 1
        self._log("info", f"camera scan done, {len(found)} found")

    def _act_open_camera(self, name: str, index: int | str, width: int = 640, height: int = 480,
                         fps: int = 30, fourcc: str | None = None) -> None:
        self._cameras.open(name, index, width=width, height=height, fps=fps, fourcc=fourcc or None)
        self._publish_cameras()
        self._log("info", f"camera '{name}' opened (index {index})")

    def _act_close_camera(self, name: str) -> None:
        self._cameras.close(name)
        self._publish_cameras()

    def _publish_cameras(self) -> None:
        with self._state_lock:
            self._state["cameras"] = self._cameras.status()
            self._state["seq"] += 1

    # ------------------------------------------------------------------ commands: calibration

    def _act_calib_prepare(self, role: str) -> None:
        session = self._require_session(role)
        session.prepare_for_calibration()
        self._draft[role] = None
        self._sweep[role] = None
        with self._state_lock:
            self._state["calib"] = {
                "role": role, "phase": "prepared", "sweep": {}, "draft": {},
                "findings": [], "flat_joints": [], "applied": False, "saved_path": None,
            }
            self._state["seq"] += 1
        self._log("info", f"{role}: torque released, position mode set — ready to calibrate")

    def _act_calib_set_homing(self, role: str) -> None:
        session = self._require_session(role)
        offsets = session.set_half_turn_homings()
        with self._state_lock:
            self._state["calib"]["phase"] = "homed"
            self._state["calib"]["homing_offsets"] = offsets
            self._state["seq"] += 1
        self._log("info", f"{role}: midpoint set — {offsets}")

    def _act_calib_sweep_start(self, role: str) -> None:
        session = self._require_session(role)
        if self._state_phase() != "homed":
            raise RuntimeError("Set the midpoint before sweeping.")
        sweep = SweepRecorder(session)
        sweep.start()
        self._sweep[role] = sweep
        with self._state_lock:
            self._state["calib"]["phase"] = "sweeping"
            self._state["calib"]["role"] = role
            self._state["calib"]["sweep"] = sweep.table()
            self._state["seq"] += 1
        self._log("info", "sweep started — push every joint to both hard stops")

    def _sweep_step(self) -> None:
        """Called by the main loop while a sweep is active."""
        role = next((r for r, s in self._sweep.items() if s is not None), None)
        if role is None:
            return
        sweep = self._sweep[role]
        assert sweep is not None
        try:
            table = sweep.sample()
        except Exception as exc:  # noqa: BLE001 - a failed read must not kill the loop
            self._log("error", f"sweep read failed: {exc}")
            return
        with self._state_lock:
            self._state["calib"]["sweep"] = table
            self._state["seq"] += 1

    def _act_calib_sweep_stop(self, role: str) -> None:
        sweep = self._sweep.get(role)
        if sweep is None:
            return
        mins, maxes, flat = sweep.finish()
        self._sweep[role] = None
        cal = build_calibration(mins, maxes, sweep.session.homing_offsets)
        self._draft[role] = cal
        with self._state_lock:
            self._state["calib"].update(
                phase="review", sweep=sweep.table(), draft=cal, flat_joints=flat,
                findings=[_finding(f) for f in diagnostics.check_arm(cal)],
            )
            self._state["seq"] += 1
        if flat:
            self._log("warn", f"joints with min == max (never swept): {flat}")
        self._log("info", "sweep finished — review the draft")

    def _act_calib_mark(self, role: str, motor: str, which: str) -> None:
        sweep = self._sweep.get(role)
        if sweep is None:
            raise RuntimeError("No sweep is running.")
        sweep.mark(motor, which)
        with self._state_lock:
            self._state["calib"]["sweep"] = sweep.table()
            self._state["seq"] += 1

    def _act_calib_draft_set(self, role: str, motor: str, field: str, value: int) -> None:
        draft = self._draft.get(role)
        if draft is None:
            raise RuntimeError("No draft yet — run a sweep or load one first.")
        if field not in ("range_min", "range_max", "homing_offset"):
            raise ValueError(f"field must be range_min / range_max / homing_offset, got {field!r}")
        draft[motor][field] = int(value)
        with self._state_lock:
            self._state["calib"]["draft"] = draft
            self._state["seq"] += 1

    def _act_calib_draft_from(self, role: str, source: str) -> None:
        """Replace the draft from ``json`` or ``motors``."""
        if source == "json":
            cal = paths.load_calibration_json(role, self._arm_id(role))
            if cal is None:
                raise RuntimeError("Calibration file not found.")
        elif source == "motors":
            session = self._require_session(role)
            cal, errors = session.read_calibration()
            if errors:
                raise RuntimeError(f"Some motors did not respond: {errors}")
        else:
            raise ValueError(f"source must be json / motors, got {source!r}")
        self._draft[role] = cal
        with self._state_lock:
            self._state["calib"]["draft"] = cal
            self._state["calib"]["findings"] = [_finding(f) for f in diagnostics.check_arm(cal)]
            self._state["calib"]["phase"] = "review"
            self._state["seq"] += 1

    def _act_calib_apply(self, role: str, write_motors: bool = True, write_json: bool = True) -> None:
        draft = self._draft.get(role)
        if draft is None:
            raise RuntimeError("Nothing to write — no draft.")
        session = self._sessions.get(role)

        if write_motors:
            session = self._require_session(role)
            session.apply_calibration(draft)
            # Read back immediately: EEPROM silently rejects writes while Lock is set.
            readback, errors = session.read_calibration()
            bad = [
                m for m, cal in draft.items()
                if m in readback
                and (int(readback[m]["range_min"]) != int(cal["range_min"])
                     or int(readback[m]["range_max"]) != int(cal["range_max"])
                     or int(readback[m]["homing_offset"]) != int(cal["homing_offset"]))
            ]
            if errors or bad:
                raise RuntimeError(
                    f"Read-back mismatch after writing (EEPROM may be locked): "
                    f"unreadable={errors}, mismatched={bad}"
                )

        saved = None
        if write_json:
            saved = paths.save_calibration_json(role, self._arm_id(role), draft)

        with self._state_lock:
            self._state["calib"]["phase"] = "applied"
            self._state["calib"]["applied"] = True
            self._state["calib"]["saved_path"] = str(saved) if saved else None
            self._state["seq"] += 1
        if session is not None:
            self._refresh_arm_findings(role)
        self._log("info", f"{role}: calibration written" + (f" and saved to {saved}" if saved else ""))

    # ------------------------------------------------------------------ commands: teleop

    def _act_set_rerun(self, enabled: bool) -> None:
        """Start or stop streaming to the Rerun viewer.

        Equals ``lerobot-teleoperate --display_data=true``: joint time series plus
        camera images in the Rerun desktop viewer. Spawning happens on the sink's
        own thread, so this returns immediately.
        """
        if enabled:
            self._rerun.start(list(SO101_LAYOUT))
            self._log("info", "rerun: starting the viewer…")
        else:
            self._rerun.stop()
            self._log("info", "rerun: streaming stopped")
        self._publish_rerun()

    def _publish_rerun(self) -> None:
        with self._state_lock:
            self._state["rerun"] = self._rerun.status
            self._state["seq"] += 1

    def _rerun_payload(self, leader: dict, follower: dict) -> tuple[dict, dict]:
        """Collect the frames the Rerun sink expects."""
        joints = {m: (leader.get(m), follower.get(m)) for m in SO101_LAYOUT}
        cameras: dict[str, tuple[bytes, float]] = {}
        for name in self._cameras.names():
            slot = self._cameras.get(name)
            if slot is None:
                continue
            jpeg = slot.jpeg()
            if jpeg:
                cameras[name] = (jpeg, slot.frame_stamp)
        return joints, cameras

    def _act_set_torque(self, role: str, on: bool) -> None:
        session = self._require_session(role)
        session.set_torque(on)
        self._update_arm(role, torque=session.torque_enabled())
        self._log("info" if on else "warn",
                  f"{role}: torque {'on' if on else 'off (the arm will drop)'}")

    def _act_teleop_start(self, release: bool = True) -> None:
        follower = self._require_session("follower")
        leader = self._require_session("leader")
        if any(self._sweep.values()):
            raise RuntimeError("A sweep is running — finish calibration first.")

        # Refuse mid-calibration: set_half_turn_homings() already changed the motor's
        # homing offsets while range_min/max are still the old ones, so driving the
        # follower with that half-applied table would send it off.
        phase = self._state_phase()
        if phase in _CALIB_IN_PROGRESS:
            raise RuntimeError(
                f"Calibration is still open (phase: {phase}). Finish it with "
                "'Write to motors + save file', or reconnect the arm to reset."
            )

        # Both arms need a complete calibration before we move anything.
        self._ensure_calibration("follower")
        self._ensure_calibration("leader")

        leader.set_torque(False)  # the leader must stay back-drivable
        # Zero the follower's goal to where it already is, then enable torque —
        # otherwise it snaps toward a stale target the moment torque comes on.
        present = follower.read_positions(normalize=False)
        follower.bus.sync_write("Goal_Position", present, normalize=False)
        follower.set_torque(True)

        self._estop.clear()
        self._teleop_release_on_stop = release
        self._teleop_fails = 0
        self._teleop_rate.clear()
        self._teleop_running = True
        with self._state_lock:
            self._state["teleop"].update(running=True, fails=0, error=None, iter=0)
            self._state["seq"] += 1
        self._log("info", "teleop started")

    def _act_teleop_stop(self) -> None:
        self._stop_teleop(release=self._teleop_release_on_stop)

    def _stop_teleop(self, release: bool) -> None:
        if not self._teleop_running:
            return
        self._teleop_running = False
        follower = self._sessions.get("follower")
        if follower is not None and release:
            try:
                follower.set_torque(False)
                self._update_arm("follower", torque=False)
            except Exception:  # noqa: BLE001
                logger.warning("failed to release torque on stop", exc_info=True)
        with self._state_lock:
            self._state["teleop"].update(running=False)
            self._state["seq"] += 1
        self._log("info", "teleop stopped" + (" (torque released — the arm will drop)" if release else " (torque held)"))

    def _act_release_all(self) -> None:
        """Panic: stop teleop and cut torque on both arms."""
        self._estop.set()
        self._teleop_running = False
        for role, session in self._sessions.items():
            try:
                session.set_torque(False)
                self._update_arm(role, torque=False)
            except Exception:  # noqa: BLE001
                logger.warning("failed to release %s", role, exc_info=True)
        with self._state_lock:
            self._state["teleop"].update(running=False)
            self._state["seq"] += 1
        self._log("warn", "release all: torque off on both arms")

    def _teleop_step(self) -> None:
        """One teleop iteration: read the leader, write the follower."""
        if self._estop.is_set():
            self._stop_teleop(release=True)
            return
        follower = self._sessions.get("follower")
        leader = self._sessions.get("leader")
        if follower is None or leader is None:
            self._stop_teleop(release=True)
            return

        t0 = time.perf_counter()
        try:
            action = leader.read_positions(normalize=True)
            follower.bus.sync_write("Goal_Position", dict(action), normalize=True)
            present = follower.read_positions(normalize=True)
        except Exception as exc:  # noqa: BLE001
            self._teleop_fails += 1
            with self._state_lock:
                self._state["teleop"].update(fails=self._teleop_fails, error=str(exc))
            if self._teleop_fails >= TELEOP_MAX_FAILS:
                self._log("error", f"teleop failed {self._teleop_fails}x in a row, stopping: {exc}")
                self._stop_teleop(release=True)
            return

        dt = time.perf_counter() - t0
        self._teleop_fails = 0
        self._teleop_rate.append(dt)
        hz = 1.0 / (sum(self._teleop_rate) / len(self._teleop_rate)) if self._teleop_rate else 0.0

        rows = []
        for motor in follower.bus.motors:
            lead, foll = action.get(motor), present.get(motor)
            rows.append({
                "joint": motor,
                "leader": None if lead is None else round(float(lead), 2),
                "follower": None if foll is None else round(float(foll), 2),
                "err": None if lead is None or foll is None else round(float(lead) - float(foll), 2),
            })

        with self._state_lock:
            self._state["teleop"].update(
                running=True, hz=round(hz, 1), rows=rows, fails=0, error=None,
                iter=self._state["teleop"]["iter"] + 1,
            )
            self._state["seq"] += 1

        rerun_status = self._rerun.status
        if rerun_status["enabled"]:
            joints, cameras = self._rerun_payload(action, present)
            self._rerun.log(joints, cameras)
            # Publish only when the sink's status actually changes (e.g. the viewer
            # finished starting up), not on every teleop iteration.
            with self._state_lock:
                changed = self._state["rerun"] != self._rerun.status
            if changed:
                self._publish_rerun()

    # ------------------------------------------------------------------ helpers

    def _require_session(self, role: str) -> ArmSession:
        session = self._sessions.get(role)
        if session is None or not session.connected:
            raise RuntimeError(f"{role} is not connected")
        return session


__all__ = ["ConsoleCore", "SO101_LAYOUT"]
