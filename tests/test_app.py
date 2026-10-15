"""Smoke test: the whole site boots, all three data channels work, shutdown releases
resources.

No hardware involved — the point is that with no arms attached the UI reports
"disconnected" rather than returning 500.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

uvicorn = pytest.importorskip("uvicorn")

from app import main as app_main  # noqa: E402

HOST = "127.0.0.1"
PORT = 8731
BASE = f"http://{HOST}:{PORT}"


@pytest.fixture(scope="module")
def server():
    """Run a real uvicorn in a background thread, then shut it down cleanly."""
    config = uvicorn.Config(app_main.app, host=HOST, port=PORT, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, name="smoke-uvicorn", daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        if srv.started:
            break
        time.sleep(0.05)
    else:
        pytest.fail("uvicorn did not start within 20 s")

    yield srv

    srv.should_exit = True
    thread.join(timeout=10)


def get(path: str) -> tuple[int, str, str]:
    """Return ``(status, body, content-type)``."""
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.status, r.read().decode("utf-8"), r.headers.get("content-type", "")


def post(path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_index_serves_the_single_page(server):
    status, body, ctype = get("/")
    assert status == 200
    assert "text/html" in ctype
    # All three tabs and the panic button must be present.
    for anchor in ("Device", "Calibration", "Teleop", "btn-panic"):
        assert anchor in body
    # The favicon the page references must be served from the same tree.
    assert "/static/assets/favicon.svg" in body


def test_static_assets_are_served(server):
    for path in ("/static/app.js", "/static/style.css",
                 "/static/vendor/pico.min.css", "/static/assets/favicon.svg"):
        status, body, _ = get(path)
        assert status == 200, f"{path} did not serve"
        assert body.strip(), f"{path} is empty"


def test_ui_is_english_only(server):
    """The interface is English-only; no Chinese may reach the browser.

    A stray CJK character in the shipped HTML/JS means a translation artefact was
    left behind. The diagnostic wording lives in a single MESSAGES map in app.js,
    so this is also a check that nothing bypassed it.
    """
    import re

    han = re.compile(r"[一-鿿]")
    for path in ("/", "/operator", "/static/app.js", "/static/style.css"):
        _, body, _ = get(path)
        found = sorted(set(han.findall(body)))
        assert not found, f"{path} still contains CJK characters: {found}"


def test_operator_view_is_served(server):
    """The operator view is the teleop screen meant for a second monitor."""
    status, body, ctype = get("/operator")
    assert status == 200
    assert "text/html" in ctype
    for anchor in ("SO-101 Operator", "/api/events", "/api/video/", "op-cams", "op-joints"):
        assert anchor in body, f"missing {anchor} in the operator view"
    # Its assets must be cache-busted too.
    assert "style.css?v=" in body
    assert "{{v}}" not in body


def test_operator_view_is_linked_from_the_console(server):
    _, body, _ = get("/")
    assert 'id="btn-operator"' in body
    _, js, _ = get("/static/app.js")
    assert 'window.open("/operator"' in js, "the button must open the operator window"


def test_asset_urls_are_cache_busted(server):
    """Static files live on disk but the browser caches them; a content hash on the
    URL means a reload picks up edits without a hard refresh."""
    _, body, _ = get("/")
    assert "app.js?v=" in body
    assert "style.css?v=" in body
    assert "{{v}}" not in body, "the version placeholder was not substituted"


def test_state_is_valid_json_and_reports_disconnected(server):
    status, body, ctype = get("/api/state")
    assert status == 200
    assert "application/json" in ctype
    state = json.loads(body)
    assert state["arms"]["follower"]["connected"] is False
    assert state["arms"]["leader"]["connected"] is False
    assert state["teleop"]["running"] is False
    assert state["calib"]["phase"] == "idle"
    assert isinstance(state["log"], list)


def test_events_streams_sse(server):
    """SSE must emit at least one data frame."""
    req = urllib.request.Request(f"{BASE}/api/events")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert "text/event-stream" in r.headers.get("content-type", "")
        buf = b""
        deadline = time.time() + 10
        while b"\n\n" not in buf and time.time() < deadline:
            chunk = r.read(1)
            if not chunk:
                break
            buf += chunk
    assert buf.startswith(b"data: ")
    payload = json.loads(buf.split(b"\n\n")[0][len(b"data: "):].decode())
    assert "arms" in payload


def test_unknown_command_is_rejected(server):
    status, body = post("/api/command", {"action": "definitely_not_a_command"})
    assert status == 400
    assert "unknown action" in json.loads(body)["detail"]


def test_command_without_an_action_is_rejected(server):
    status, body = post("/api/command", {})
    assert status == 400
    assert "missing" in json.loads(body)["detail"]


def test_refresh_ports_populates_state(server):
    """refresh_ports is the only command that produces real data without hardware."""
    status, _ = post("/api/command", {"action": "refresh_ports"})
    assert status == 200

    deadline = time.time() + 10
    while time.time() < deadline:
        _, body, _ = get("/api/state")
        if json.loads(body)["ports"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("ports is still empty after refresh_ports")

    ports = json.loads(body)["ports"]
    assert all({"device", "description", "hwid", "serial_number"} <= set(p) for p in ports)


def test_estop_endpoint_is_not_queued(server):
    """The e-stop has its own endpoint so it never waits behind a slow bus call."""
    status, body = post("/api/estop", {})
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_connect_without_hardware_fails_gracefully(server):
    """Connecting to a non-existent port must land in last_error, not kill the service."""
    baseline = json.loads(get("/api/state")[1])["last_error"]
    status, _ = post("/api/command", {"action": "connect_arm",
                                      "role": "follower", "port": "COM_NOT_REAL",
                                      "arm_id": "nope"})
    assert status == 200  # accepted for execution

    err = wait_for_error(baseline, timeout=15)
    # Assert **which** error: "there was an error" is too loose, and a missing
    # dependency's ImportError would satisfy it — that is how the deepdiff bug hid.
    assert "No module named" not in err
    assert "ImportError" not in err
    assert json.loads(get("/api/state")[1])["arms"]["follower"]["connected"] is False
    # The service must still be alive.
    assert get("/api/state")[0] == 200


def wait_for_error(baseline: str | None, timeout: float = 10.0) -> str:
    """Wait for last_error to change from ``baseline`` and return the new value.

    Polling for "some error" is not enough: an error left by an earlier test would
    satisfy it and the assertion would then check the wrong message.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body, _ = get("/api/state")
        err = json.loads(body)["last_error"]
        if err and err != baseline:
            return err
        time.sleep(0.05)
    pytest.fail("last_error never changed")


def test_compare_arms_without_files_is_reported(server):
    """A missing calibration file must be a readable error, not a 500."""
    baseline = json.loads(get("/api/state")[1])["last_error"]
    status, _ = post("/api/command", {"action": "compare_arms"})
    assert status == 200
    assert "calibration files" in wait_for_error(baseline)


def test_video_for_an_unopened_camera_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as ei:
        get("/api/video/nope")
    assert ei.value.code == 404


def test_shutdown_releases_everything(server):
    """After shutdown the core must hold no sessions and no cameras."""
    app_main.core.shutdown()
    assert app_main.core._sessions == {}          # noqa: SLF001
    assert app_main.core._cameras.names() == []   # noqa: SLF001
    # Idempotent: a second shutdown must not blow up.
    app_main.core.shutdown()
