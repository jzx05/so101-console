"""Calibration diagnostics - pure functions, no hardware, no third-party imports.

Every rule here was derived from a real debugging session, not picked arbitrarily.

Background:

* The Feetech STS3215 has a 12-bit encoder: 4096 counts per revolution, so
  ``1 count ~= 0.0879 deg``.
* Normalization modes (see upstream ``motors_bus.py`` ``_normalize``):

  - Body joints use ``MotorNormMode.DEGREES``: ``n = (raw - mid) * 360/4095``,
    where ``mid = (min + max) / 2``.
  - The gripper uses ``MotorNormMode.RANGE_0_100``:
    ``n = (raw - min) / (max - min) * 100``.

* A sweep records the *observed* extremes, so **a span can never exceed the true
  mechanical travel**. The largest span seen across repeated sweeps is therefore
  the best lower bound on the real travel, and the only sound basis for judging
  whether a joint was pushed to both hard stops.

Messages are intentionally NOT built here. A :class:`Finding` carries a machine
readable ``code`` plus the numbers behind the verdict, and the web UI renders the
wording (in English or Chinese). That keeps this module pure and translatable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Full-scale counts of the 12-bit encoder (``model_resolution_table["sts3215"] - 1``).
RESOLUTION = 4095

#: Counts per degree.
COUNTS_PER_DEG = RESOLUTION / 360.0  # 11.375


def counts_to_deg(counts: float) -> float:
    """Convert encoder counts to degrees."""
    return counts / COUNTS_PER_DEG


@dataclass(frozen=True)
class JointSpec:
    """Per-joint thresholds.

    Attributes:
        name: Joint name, matching the calibration JSON key.
        norm_mode: ``"degrees"`` or ``"range_0_100"``; decides the semantics.
        is_body: Whether this is a body joint. Only body joints are comparable
            across the two arms.
        is_full_turn: Full-turn joint (``wrist_roll``), whose range is forced to
            0/4095 and therefore skipped by span/wrap/mid checks.
        min_span: Span below this is a failure.
        warn_span: Span below this is a warning.
    """

    name: str
    norm_mode: str
    is_body: bool
    is_full_turn: bool = False
    min_span: int = 2000
    warn_span: int = 2200


#: SO-101 joint layout, matching the upstream bus definition (ids 1..6).
#: The gripper gets much lower thresholds on purpose: it is a parallel jaw /
#: trigger whose mechanical travel is inherently shorter than a body joint's.
SO101_JOINTS: tuple[JointSpec, ...] = (
    JointSpec("shoulder_pan", "degrees", is_body=True),
    JointSpec("shoulder_lift", "degrees", is_body=True),
    JointSpec("elbow_flex", "degrees", is_body=True),
    JointSpec("wrist_flex", "degrees", is_body=True),
    JointSpec("wrist_roll", "degrees", is_body=True, is_full_turn=True),
    JointSpec("gripper", "range_0_100", is_body=False, min_span=800, warn_span=1000),
)

SPECS: dict[str, JointSpec] = {s.name: s for s in SO101_JOINTS}

#: Minimum distance from 0 / 4095 for the encoder window. Crossing zero makes the
#: reading jump a full turn.
WRAP_MARGIN = 250

#: Tolerance for how far ``(min + max) / 2`` may sit from 2047. This only reflects
#: wrap headroom, never the normalized mapping.
MID_TOLERANCE = 150

#: Span difference between the two arms (counts) above which we warn. ~13 degrees.
PAIR_SPAN_TOLERANCE = 150

#: Thresholds for the constant offset that a span difference injects into teleop.
PAIR_OFFSET_WARN_DEG = 5.0
PAIR_OFFSET_FAIL_DEG = 15.0


@dataclass(frozen=True)
class Finding:
    """One diagnostic verdict, rendered by the UI.

    Attributes:
        joint: Joint name; arm-level verdicts use ``"-"``.
        severity: ``"ok"`` / ``"info"`` / ``"warn"`` / ``"fail"``.
        code: Machine-readable rule id, used as the i18n message key.
        data: The numbers behind the verdict, shown as detail columns.
    """

    joint: str
    severity: str
    code: str
    data: dict = field(default_factory=dict)


def _span(cal: dict) -> int:
    return int(cal["range_max"]) - int(cal["range_min"])


def _mid(cal: dict) -> float:
    return (int(cal["range_min"]) + int(cal["range_max"])) / 2


def check_joint(name: str, cal: dict, spec: JointSpec | None = None) -> list[Finding]:
    """Check one joint's span, wrap headroom and midpoint.

    Args:
        name: Joint name.
        cal: ``{"range_min": int, "range_max": int, ...}``.
        spec: Thresholds for this joint; defaults to :data:`SPECS`.

    Returns:
        All verdicts for this joint.
    """
    spec = spec or SPECS.get(name, JointSpec(name, "degrees", is_body=False))
    out: list[Finding] = []
    lo, hi = int(cal["range_min"]), int(cal["range_max"])
    span = hi - lo
    data = {"range_min": lo, "range_max": hi, "span": span, "span_deg": round(counts_to_deg(span), 1)}

    if span == 0:
        return [Finding(name, "fail", "MIN_EQ_MAX", data)]

    # --- span ---
    if spec.is_full_turn:
        # A full-turn joint has its range forced to 0/4095. Span, wrap headroom and
        # midpoint are all consequences of that, so applying body-joint thresholds
        # here would produce three guaranteed false positives. Stop early.
        return [Finding(name, "ok", "SPAN_FULL_TURN", data)]

    if span < spec.min_span:
        out.append(Finding(name, "fail", "SPAN_SHORT", data | {"min_span": spec.min_span}))
    elif span < spec.warn_span:
        out.append(Finding(name, "warn", "SPAN_LOW", data | {"warn_span": spec.warn_span}))
    else:
        out.append(Finding(name, "ok", "SPAN_OK", data))

    # --- wrap headroom ---
    head_low, head_high = lo, RESOLUTION - hi
    data |= {"headroom_low": head_low, "headroom_high": head_high,
             "headroom_low_deg": round(counts_to_deg(head_low), 1),
             "headroom_high_deg": round(counts_to_deg(head_high), 1),
             "wrap_margin": WRAP_MARGIN}
    if head_low < WRAP_MARGIN:
        out.append(Finding(name, "warn", "WRAP_LOW", data))
    if head_high < WRAP_MARGIN:
        out.append(Finding(name, "warn", "WRAP_HIGH", data))

    # --- midpoint ---
    mid = _mid(cal)
    dev = mid - RESOLUTION / 2
    if abs(dev) > MID_TOLERANCE:
        out.append(
            Finding(
                name,
                "info",
                "MID_OFFSET",
                data | {"mid": round(mid, 1), "mid_dev": round(dev, 1),
                        "mid_dev_deg": round(counts_to_deg(dev), 1)},
            )
        )

    return out


def check_arm(calibration: dict[str, dict]) -> list[Finding]:
    """Run :func:`check_joint` over every joint of one arm."""
    out: list[Finding] = []
    for name, cal in calibration.items():
        out.extend(check_joint(name, cal))
    return out


def check_pair(leader: dict[str, dict], follower: dict[str, dict]) -> list[Finding]:
    """Compare the two arms. Only the **five body joints** are meaningful here.

    Why the gripper is skipped: both arms' grippers use ``RANGE_0_100``, so they map
    as "each arm's own percentage of its own range". Their spans are not supposed to
    match. Body joints use ``DEGREES``, where counts map one-to-one.

    Why the offset is **half the span difference** rather than a difference of the
    two midpoints: the two arms' raw encoder frames are NOT aligned (different motor
    mounting, different homing offsets), so raw values cannot be subtracted across
    arms. Derivation:

        Leader normalizes ``n = (raw_L - mid_L) * 360/4095``; the follower
        un-normalizes ``target = n * 4095/360 + mid_F``, i.e.
        ``target = raw_L - mid_L + mid_F``.
        Let ``D = raw_L(theta) - raw_F(theta)`` be the constant mounting difference
        between the arms. Mirroring is exact iff ``mid_L - mid_F == D``.
        If the leader's span is short by ``delta`` at one end, then
        ``mid_L - mid_F = D - delta/2``, i.e. a constant ``delta/2`` counts of error.

    So ``(span_L - span_F) / 2`` is the angle the follower is actually dragged off
    by, and at each end the follower is commanded past its own limit and clamped by
    the firmware.

    Args:
        leader: Leader calibration table.
        follower: Follower calibration table.

    Returns:
        Arm-level verdicts.
    """
    out: list[Finding] = []
    for spec in SO101_JOINTS:
        if not spec.is_body or spec.is_full_turn:
            continue
        cal_l, cal_f = leader.get(spec.name), follower.get(spec.name)
        if cal_l is None or cal_f is None:
            continue

        span_l, span_f = _span(cal_l), _span(cal_f)
        delta = span_l - span_f
        offset_deg = counts_to_deg(delta / 2)
        data = {
            "leader_span": span_l,
            "follower_span": span_f,
            "leader_span_deg": round(counts_to_deg(span_l), 1),
            "follower_span_deg": round(counts_to_deg(span_f), 1),
            "delta": delta,
            "delta_deg": round(counts_to_deg(delta), 1),
            "offset_deg": round(offset_deg, 1),
            "reach_deg": round(abs(counts_to_deg(delta / 2)), 1),
            "tolerance": PAIR_SPAN_TOLERANCE,
        }

        if abs(delta) <= PAIR_SPAN_TOLERANCE:
            out.append(Finding(spec.name, "ok", "PAIR_OK", data))
            continue

        out.append(Finding(spec.name, "warn", "SPAN_MISMATCH", data | {"shorter": "leader" if delta < 0 else "follower"}))
        sev = "fail" if abs(offset_deg) > PAIR_OFFSET_FAIL_DEG else "warn"
        out.append(Finding(spec.name, sev, "PAIR_MID_OFFSET", data))
        out.append(Finding(spec.name, "warn", "REACH_CLIP", data))
    return out


def check_motors(
    expected: dict[int, int],
    found: dict[int, int] | None,
    spec_by_id: dict[int, JointSpec] | None = None,
) -> list[Finding]:
    """Check which motors answer on the bus.

    Args:
        expected: ``{motor_id: model_number}``; SO-101 is ``{1..6: 777}``.
        found: Result of the enumeration; ``None`` means nothing answered at all.
        spec_by_id: Optional ``{motor_id: JointSpec}`` to translate ids to joint names.

    Returns:
        Per-motor verdicts.
    """
    spec_by_id = spec_by_id or {i + 1: s for i, s in enumerate(SO101_JOINTS)}
    if found is None:
        return [Finding("-", "fail", "BUS_DEAD", {})]

    out: list[Finding] = []
    for mid, model in sorted(expected.items()):
        name = spec_by_id[mid].name if mid in spec_by_id else f"id{mid}"
        if mid not in found:
            out.append(Finding(name, "fail", "MOTOR_MISSING", {"id": mid, "expected_model": model}))
        elif found[mid] != model:
            out.append(
                Finding(name, "fail", "MODEL_MISMATCH",
                        {"id": mid, "found_model": found[mid], "expected_model": model})
            )
        else:
            out.append(Finding(name, "ok", "MOTOR_OK", {"id": mid, "model": model}))

    extra = sorted(set(found) - set(expected))
    if extra:
        out.append(Finding("-", "info", "MOTOR_EXTRA", {"extra_ids": extra}))
    return out


def summarize(findings: list[Finding]) -> dict[str, int]:
    """Count findings per severity, for a one-line summary in the UI."""
    counts = {"ok": 0, "info": 0, "warn": 0, "fail": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
