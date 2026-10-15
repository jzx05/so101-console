"""Regression tests for the diagnostic rules, using two real calibration files
captured during the 2026-10-15 debugging session.

These numbers are not invented: `tests/data/leader.json` is a real snapshot of the
leader arm's shoulder_pan being repeatedly under-swept, while `follower.json` is
the follower's healthy state after re-calibration. Catching exactly that
discrepancy by eye was the whole reason this tool exists, so it makes the right
regression fixture.

Findings carry a machine-readable `code` plus the numbers behind the verdict; the
wording lives in the UI. Tests therefore assert on code + data, not on prose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.diagnostics import (
    SO101_JOINTS,
    SPECS,
    Finding,
    check_arm,
    check_joint,
    check_motors,
    check_pair,
    counts_to_deg,
    summarize,
)

DATA = Path(__file__).parent / "data"


@pytest.fixture
def leader() -> dict[str, dict]:
    return json.loads((DATA / "leader.json").read_text(encoding="utf-8"))


@pytest.fixture
def follower() -> dict[str, dict]:
    return json.loads((DATA / "follower.json").read_text(encoding="utf-8"))


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def by_code(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


# --------------------------------------------------------------------------- pair compare


def test_pair_flags_shoulder_pan(leader, follower):
    """The real defect: leader pan spans 2036, follower 2415 — a 379-count gap.

    This is the verdict that used to require eyeballing two terminal tables.
    """
    out = check_pair(leader, follower)

    mismatches = by_code(out, "SPAN_MISMATCH")
    assert len(mismatches) == 1
    assert mismatches[0].joint == "shoulder_pan"
    assert mismatches[0].data["delta"] == 2036 - 2415 == -379
    assert mismatches[0].data["shorter"] == "leader"

    # The offset is half the span difference, not a subtraction of the two
    # midpoints — the two arms' raw frames are not aligned.
    offset = by_code(out, "PAIR_MID_OFFSET")
    assert len(offset) == 1
    assert offset[0].severity == "fail"
    assert offset[0].data["offset_deg"] == pytest.approx(-16.7, abs=0.1)
    assert abs(offset[0].data["offset_deg"]) > 15.0

    clip = by_code(out, "REACH_CLIP")
    assert len(clip) == 1
    assert clip[0].data["reach_deg"] == pytest.approx(16.7, abs=0.1)


def test_pair_passes_the_other_body_joints(leader, follower):
    """The remaining three body joints are within 150 counts and must not be flagged."""
    out = check_pair(leader, follower)
    flagged = {f.joint for f in out if f.code in {"SPAN_MISMATCH", "PAIR_MID_OFFSET", "REACH_CLIP"}}
    assert flagged == {"shoulder_pan"}

    ok = {f.joint for f in out if f.code == "PAIR_OK"}
    assert ok == {"shoulder_lift", "elbow_flex", "wrist_flex"}


def test_pair_skips_gripper_and_wrist_roll(leader, follower):
    """Both grippers are RANGE_0_100, so their spans are not supposed to match."""
    out = check_pair(leader, follower)
    assert not [f for f in out if f.joint in {"gripper", "wrist_roll"}]

    # Counter-check: the raw gripper span difference is large, so including it
    # would produce a guaranteed false positive.
    gripper_delta = (
        leader["gripper"]["range_max"] - leader["gripper"]["range_min"]
        - (follower["gripper"]["range_max"] - follower["gripper"]["range_min"])
    )
    assert gripper_delta == 1195 - 1286 == -91


# --------------------------------------------------------------------------- single joint


def test_leader_pan_wrap_headroom(leader):
    """Leader pan's low end is 203 — only 203 counts from the encoder zero."""
    out = check_joint("shoulder_pan", leader["shoulder_pan"])
    assert "WRAP_LOW" in codes(out)
    wrap = by_code(out, "WRAP_LOW")[0]
    assert wrap.severity == "warn"
    assert wrap.data["headroom_low"] == 203
    assert wrap.data["headroom_low"] < 250


def test_leader_pan_span_is_short(leader):
    """2036 counts = 179 degrees, below the usual 2200."""
    out = check_joint("shoulder_pan", leader["shoulder_pan"])
    short = by_code(out, "SPAN_LOW")
    assert len(short) == 1
    assert short[0].severity == "warn"
    assert short[0].data["span"] == 2036
    assert short[0].data["span_deg"] == pytest.approx(179.0, abs=0.5)


def test_follower_arm_has_no_failures(follower):
    """The follower is healthy after re-calibration — the payoff of the earlier fixes."""
    out = check_arm(follower)
    assert [f for f in out if f.severity == "fail"] == []
    assert by_code(out, "WRAP_LOW") == []
    assert by_code(out, "WRAP_HIGH") == []


def test_wrist_roll_checks_are_skipped():
    """The full-turn joint is forced to 0/4095 and must produce exactly one verdict."""
    out = check_joint("wrist_roll", {"range_min": 0, "range_max": 4095}, SPECS["wrist_roll"])
    assert codes(out) == {"SPAN_FULL_TURN"}
    assert out[0].severity == "ok"


def test_min_eq_max_is_fatal():
    out = check_joint("elbow_flex", {"range_min": 2000, "range_max": 2000})
    assert codes(out) == {"MIN_EQ_MAX"}
    assert out[0].severity == "fail"


def test_old_follower_wrap_problem_is_detected():
    """An earlier follower build had elbow max at 3963, only 132 counts from 4095.

    That data was replaced by the 13:10 re-calibration, but the rule must still fire.
    """
    out = check_joint("elbow_flex", {"range_min": 1742, "range_max": 3963})
    wrap = by_code(out, "WRAP_HIGH")
    assert len(wrap) == 1
    assert wrap[0].data["headroom_high"] == 4095 - 3963 == 132


def test_span_short_is_a_failure_not_a_warning():
    """A span barely above zero means the joint was never swept at all."""
    out = check_joint("elbow_flex", {"range_min": 1000, "range_max": 1500})
    short = by_code(out, "SPAN_SHORT")
    assert len(short) == 1
    assert short[0].severity == "fail"


# --------------------------------------------------------------------------- motor presence


def test_missing_gripper_is_reported_not_raised():
    """Reproduces the real 2026-10-15 12:52 failure: only IDs 1..5 on COM24.

    Upstream's `_assert_motors_exist` raises RuntimeError here; a health check needs
    to say which motor is missing instead.
    """
    expected = {i + 1: 777 for i in range(6)}
    found = {1: 777, 2: 777, 3: 777, 4: 777, 5: 777}

    out = check_motors(expected, found)
    missing = by_code(out, "MOTOR_MISSING")
    assert len(missing) == 1
    assert missing[0].joint == "gripper"
    assert missing[0].data["id"] == 6
    assert len([f for f in out if f.severity == "ok"]) == 5


def test_wrong_model_is_reported():
    expected = {i + 1: 777 for i in range(6)}
    found = {**expected, 3: 1284}
    out = check_motors(expected, found)
    bad = by_code(out, "MODEL_MISMATCH")
    assert len(bad) == 1
    assert bad[0].joint == "elbow_flex"
    assert bad[0].data["found_model"] == 1284


def test_dead_bus_is_reported():
    out = check_motors({i + 1: 777 for i in range(6)}, None)
    assert len(out) == 1
    assert out[0].severity == "fail"
    assert out[0].code == "BUS_DEAD"


def test_unexpected_ids_are_noted():
    expected = {i + 1: 777 for i in range(6)}
    found = {**expected, 9: 777}
    out = check_motors(expected, found)
    extra = by_code(out, "MOTOR_EXTRA")
    assert len(extra) == 1
    assert extra[0].data["extra_ids"] == [9]


# --------------------------------------------------------------------------- misc


def test_every_finding_has_a_translatable_code(leader):
    """The UI renders wording from `code`, so an unknown code shows raw text.

    This guards the one contract between the Python rules and the i18n table.
    """
    known = {
        "MIN_EQ_MAX", "SPAN_FULL_TURN", "SPAN_SHORT", "SPAN_LOW", "SPAN_OK",
        "WRAP_LOW", "WRAP_HIGH", "MID_OFFSET", "PAIR_OK", "SPAN_MISMATCH",
        "PAIR_MID_OFFSET", "REACH_CLIP", "BUS_DEAD", "MOTOR_MISSING",
        "MODEL_MISMATCH", "MOTOR_OK", "MOTOR_EXTRA",
    }
    emitted = {f.code for f in check_arm(leader)} | {f.code for f in check_motors({1: 777}, {1: 777})}
    assert emitted <= known, f"codes missing from the i18n table: {emitted - known}"


def test_findings_are_json_serializable(leader):
    from dataclasses import asdict
    json.dumps([asdict(f) for f in check_arm(leader)])


def test_summarize_counts_every_severity(leader):
    out = check_arm(leader)
    counts = summarize(out)
    assert set(counts) == {"ok", "info", "warn", "fail"}
    assert sum(counts.values()) == len(out)


def test_counts_to_deg_matches_encoder_resolution():
    assert counts_to_deg(4095) == pytest.approx(360.0)
    assert counts_to_deg(1) == pytest.approx(0.0879, abs=1e-4)


def test_joint_specs_cover_the_so101_layout():
    assert [s.name for s in SO101_JOINTS] == [
        "shoulder_pan", "shoulder_lift", "elbow_flex",
        "wrist_flex", "wrist_roll", "gripper",
    ]
    assert [s.is_body for s in SO101_JOINTS] == [True, True, True, True, True, False]
