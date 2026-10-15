"""Travel sweep — replaces upstream's ``record_ranges_of_motion``.

That method (``motors_bus.py:803``) polls stdin via ``enter_pressed()`` to decide
when to stop, and on Windows ``enter_pressed`` uses ``msvcrt.kbhit()``, which
raises ``OSError`` when stdin is not a console. A browser has no stdin and should
not need one. This keeps the same min/max semantics but is driven by a UI button.

The semantics match upstream deliberately (see ``motors_bus.py:822-850``):

* The first frame seeds min/max — starting from 0 would make an untouched joint
  look like it had been swept.
* Full-turn joints are excluded and fixed to 0/4095.
* ``normalize=False`` is mandatory: ``set_half_turn_homings()`` has already cleared
  the bus calibration cache by this point.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hardware import ArmSession

#: Full-turn joint; its range is written as a constant. Matches `so_follower.py:159`.
FULL_TURN_MOTOR = "wrist_roll"

#: Sampling period. Matches the 0.02 s of upstream `motors_bus.py:846`.
DEFAULT_SAMPLE_HZ = 50.0


class SweepRecorder:
    """Samples joint positions continuously and accumulates the observed extremes."""

    def __init__(
        self,
        session: ArmSession,
        motors: list[str] | None = None,
        full_turn_motor: str = FULL_TURN_MOTOR,
        sample_hz: float = DEFAULT_SAMPLE_HZ,
    ):
        """
        Args:
            session: A connected arm.
            motors: Joints to sweep; ``None`` means all except the full-turn joint.
            full_turn_motor: Joint whose range is pinned to 0/4095.
            sample_hz: Upper bound on the sampling rate.
        """
        self.session = session
        self.full_turn_motor = full_turn_motor
        self.period = 1.0 / sample_hz
        all_motors = list(session.bus.motors) if session.bus else []
        self.motors = motors if motors is not None else [m for m in all_motors if m != full_turn_motor]
        self.mins: dict[str, int] = {}
        self.maxes: dict[str, int] = {}
        self.last: dict[str, int] = {}
        self.samples = 0
        self.started_at = 0.0
        self._next_sample = 0.0

    def start(self) -> dict[str, dict]:
        """Read the seed frame and return the initial table."""
        pos = self.session.read_positions(normalize=False)
        self.mins = {m: int(pos[m]) for m in self.motors}
        self.maxes = {m: int(pos[m]) for m in self.motors}
        self.last = {m: int(pos[m]) for m in self.motors}
        self.samples = 0
        self.started_at = time.perf_counter()
        self._next_sample = 0.0
        return self.table()

    def sample(self) -> dict[str, dict]:
        """Take one sample (rate-limited) and return the current table."""
        now = time.perf_counter()
        if now < self._next_sample:
            return self.table()
        self._next_sample = now + self.period

        pos = self.session.read_positions(normalize=False)
        for m in self.motors:
            v = int(pos[m])
            self.last[m] = v
            if v < self.mins[m]:
                self.mins[m] = v
            if v > self.maxes[m]:
                self.maxes[m] = v
        self.samples += 1
        return self.table()

    def table(self) -> dict[str, dict]:
        """Current min / pos / max / span for every swept joint."""
        return {
            m: {
                "min": self.mins.get(m, 0),
                "pos": self.last.get(m, 0),
                "max": self.maxes.get(m, 0),
                "span": self.maxes.get(m, 0) - self.mins.get(m, 0),
                "samples": self.samples,
            }
            for m in self.motors
        }

    def mark(self, motor: str, which: str) -> None:
        """Pin a joint's min or max to its current position.

        Mirrors the "set min" / "set max" buttons of the upstream calibration GUI:
        a manual escape hatch for joints that cannot physically reach a hard stop,
        or whose reading is noisy.

        Args:
            motor: Joint name.
            which: ``"min"`` or ``"max"``.
        """
        if motor not in self.motors:
            raise KeyError(f"{motor} is not part of this sweep")
        if which == "min":
            self.mins[motor] = self.last[motor]
        elif which == "max":
            self.maxes[motor] = self.last[motor]
        else:
            raise ValueError(f"which must be 'min' or 'max', got {which!r}")

    def finish(self) -> tuple[dict[str, int], dict[str, int], list[str]]:
        """End the sweep.

        Returns:
            ``(mins, maxes, flat_joints)``. The full-turn joint is filled in with
            0/4095 here, matching `so_follower.py:166-167`. Joints whose min equals
            their max land in the third value — upstream raises on those, which is
            not something a web UI should do.
        """
        mins, maxes = dict(self.mins), dict(self.maxes)
        if self.full_turn_motor in (self.session.bus.motors if self.session.bus else {}):
            mins[self.full_turn_motor] = 0
            maxes[self.full_turn_motor] = 4095

        flat = [m for m in self.motors if mins[m] == maxes[m]]
        return mins, maxes, flat
