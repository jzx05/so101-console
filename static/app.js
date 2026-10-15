/* SO-101 debug console frontend — no framework.
 * State arrives over SSE, commands go out as POSTs.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/** Escape HTML. Port descriptions and exception text are external strings. */
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** POST a command. Returns immediately; results come back over SSE. */
async function cmd(action, args = {}) {
  const r = await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...args }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    flashError(d.detail || `${action} rejected`);
  }
}

let errorTimer = null;
function flashError(msg) {
  const el = $("#error-banner");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => { el.hidden = true; }, 8000);
}

const cfg = {
  get(key, fallback = "") {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode */ }
  },
};

/* ------------------------------------------------------------------ messages
 * Diagnostic wording. The backend emits a machine-readable code plus the numbers
 * behind the verdict; the sentence is built here. Kept in one map so the rules
 * stay pure and the wording stays in one place.
 */
const MESSAGES = {
  MIN_EQ_MAX: (f) => `${f.joint}: range_min equals range_max — this calibration is invalid. Upstream raises on it, and teleop cannot normalize.`,
  SPAN_FULL_TURN: (f) => `${f.joint} is a full-turn joint, range fixed at 0-4095. Span and wrap checks skipped.`,
  SPAN_SHORT: (f) => `${f.joint} span is only ${f.data.span} counts (${f.data.span_deg}°), below ${f.data.min_span}. One end never reached its hard stop.`,
  SPAN_LOW: (f) => `${f.joint} span is ${f.data.span} counts (${f.data.span_deg}°), under the usual ${f.data.warn_span}. Re-sweep with both ends against the stops.`,
  SPAN_OK: (f) => `${f.joint} span ${f.data.span} counts (${f.data.span_deg}°).`,
  WRAP_LOW: (f) => `${f.joint} low end is ${f.data.range_min}, only ${f.data.headroom_low} counts (${f.data.headroom_low_deg}°) from the encoder zero. Crossing zero makes the reading jump a full turn and whips the follower.`,
  WRAP_HIGH: (f) => `${f.joint} high end is ${f.data.range_max}, only ${f.data.headroom_high} counts (${f.data.headroom_high_deg}°) from 4095. Same wrap risk.`,
  MID_OFFSET: (f) => `${f.joint} span midpoint is ${f.data.mid} (${f.data.mid_dev > 0 ? "+" : ""}${f.data.mid_dev} from 2047). Only affects wrap headroom — normalization uses (min+max)/2 as its zero, not 2047.`,
  PAIR_OK: (f) => `${f.joint} spans match (Δ ${f.data.delta > 0 ? "+" : ""}${f.data.delta} counts).`,
  SPAN_MISMATCH: (f) => `${f.joint}: leader ${f.data.leader_span} vs follower ${f.data.follower_span} counts, Δ ${f.data.delta > 0 ? "+" : ""}${f.data.delta} (${f.data.delta_deg}°). The ${f.data.shorter} side was not swept to both stops.`,
  PAIR_MID_OFFSET: (f) => `${f.joint} will carry a constant ${f.data.offset_deg > 0 ? "+" : ""}${f.data.offset_deg}° offset in teleop — the follower sits at a different pose even when both arms are aligned. It comes from half the span difference.`,
  REACH_CLIP: (f) => `${f.joint} has about ${f.data.reach_deg}° at each end the leader can reach but the follower cannot; the firmware limit clips it.`,
  BUS_DEAD: () => `No motor answered on the bus. Check the power supply and the serial cable.`,
  MOTOR_MISSING: (f) => `${f.joint} (ID ${f.data.id}) did not answer. Check its 3-pin cable — the SO-101 is a daisy chain and the gripper is the last link, so any loose cable between wrist_roll and the gripper drops only that motor.`,
  MODEL_MISMATCH: (f) => `${f.joint} (ID ${f.data.id}) reports model ${f.data.found_model}, expected ${f.data.expected_model}. Wrong motor type.`,
  MOTOR_OK: (f) => `${f.joint} (ID ${f.data.id}) present, model ${f.data.model}.`,
  MOTOR_EXTRA: (f) => `Unexpected IDs also answered: ${f.data.extra_ids}.`,
};

const findingText = (f) => (MESSAGES[f.code] ? MESSAGES[f.code](f) : f.code);

const SEV_LABEL = { ok: "ok", info: "info", warn: "warn", fail: "fail" };
const SEV_ORDER = { fail: 0, warn: 1, info: 2, ok: 3 };

/* ------------------------------------------------------------------ render */

function renderFindings(el, findings) {
  if (!findings || !findings.length) {
    el.innerHTML = `<p class="hint">No findings yet — connect an arm or press Re-check.</p>`;
    return;
  }
  const rows = [...findings]
    .sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity])
    .map((f) => `<tr class="sev-${esc(f.severity)}">
        <td class="nw"><span class="sev sev-${esc(f.severity)}">${esc(SEV_LABEL[f.severity] || f.severity)}</span></td>
        <td class="nw mono">${esc(f.joint)}</td>
        <td>${esc(findingText(f))}</td>
      </tr>`).join("");
  el.innerHTML = `<table class="tight"><tbody>${rows}</tbody></table>`;
}

function renderArm(role, arm) {
  const pill = $(`#hdr-${role}`);
  const label = role === "follower" ? "Follower" : "Leader";
  pill.textContent = `${label} · ${arm.connected ? arm.port : "off"}`;
  pill.className = "pill" + (arm.connected ? " ok" : "");
  $(`#${role}-title`).textContent = arm.connected ? arm.id : "";

  const exp = arm.expected || {};
  const found = arm.motors || {};
  $(`#${role}-motors`).innerHTML = '<div class="row">' +
    Object.keys(exp).map(Number).sort((a, b) => a - b).map((id) => {
      const ok = found[id] !== undefined;
      return `<span class="pill ${ok ? "ok" : "bad"}">${id}${ok ? " ✓" : " ✗"}</span>`;
    }).join("") + "</div>";

  renderFindings($(`#${role}-findings`), arm.findings);

  $(`#${role}-json`).textContent = arm.id
    ? `${arm.json_exists ? "calibration file present" : "no calibration file"} · ${arm.json_path}`
    : "";
}

/** Populate a port dropdown.

 * Rebuilds only when the device list actually changes: render() runs on every SSE
 * tick (~20 Hz) and rewriting innerHTML that often would close an open dropdown and
 * discard the user's selection.
 */
let portSignature = null;
function fillPortSelects(ports) {
  const list = ports || [];
  const sig = list.map((p) => p.device).join(",");
  // Also fill when a select is still empty: on the very first render the port list
  // has not arrived yet, and comparing signatures alone would leave it blank.
  const alreadyFilled = $("#follower-port").options.length > 0;
  if (sig === portSignature && alreadyFilled) return;
  portSignature = sig;

  // A built-in port can never drive a Feetech bus (it cannot reach 1 Mbaud), so
  // defaulting to the first entry in the list would default to something that
  // cannot work. Prefer a USB adapter when one is present.
  const preferred = (list.find((p) => p.kind === "USB adapter") || list[0] || {}).device || "";

  ["follower", "leader"].forEach((role) => {
    const sel = $(`#${role}-port`);
    const remembered = cfg.get(`port.${role}`);
    sel.innerHTML = list.map((p) =>
      `<option value="${esc(p.device)}">${esc(p.device)}${p.description ? " — " + esc(p.description) : ""}</option>`
    ).join("") || '<option value="">no ports found</option>';
    sel.value = (remembered && list.some((p) => p.device === remembered)) ? remembered : preferred;
  });
}

function renderPorts(ports, probes) {
  probes = probes || {};
  const rows = (ports || []).map((p) => {
    const pr = probes[p.device];
    let verdict = `<span class="muted">not probed</span>`;
    if (pr) {
      if (pr.ok) {
        const ids = Object.keys(pr.motors);
        const cls = ids.length === 6 ? "sev-ok" : "sev-warn";
        verdict = `<span class="${cls}">${ids.length}/6 motors · ${esc(pr.elapsed_ms)} ms</span>`
                + ` <span class="muted">IDs ${esc(ids.join(",") || "none")}</span>`;
      } else {
        verdict = `<span class="sev-fail">${esc(pr.error)}</span>`;
      }
    }
    return `<tr>
      <td class="mono nw">${esc(p.device)}</td>
      <td class="nw">${esc(p.kind || "")}</td>
      <td class="muted small">${esc(p.description)}</td>
      <td class="mono small muted">${esc(p.serial_number || p.hwid)}</td>
      <td class="small">${verdict}</td>
      <td class="nw"><button data-probe="${esc(p.device)}">Probe</button></td>
    </tr>`;
  }).join("");
  $("#tbl-ports tbody").innerHTML = rows ||
    `<tr><td colspan="6" class="muted">No serial ports found. Press Refresh.</td></tr>`;
}

function renderCameras(state) {
  const scan = state.scan || {};
  const status = $("#cam-scan-status");
  if (scan.scanning) status.textContent = "scanning…";
  else if (scan.cameras) status.textContent = `${scan.cameras.length} found`;
  else status.textContent = "";

  $("#tbl-cams tbody").innerHTML = (scan.cameras || []).map((c) => `
    <tr>
      <td class="mono nw">${esc(c.index)}</td>
      <td class="mono nw">${esc(c.width)}×${esc(c.height)}</td>
      <td class="mono">${esc(c.fps)}${c.fps === 0 ? ' <span class="muted">(reads 0 before the stream starts)</span>' : ""}</td>
      <td class="mono nw">${esc(c.fourcc)}</td>
      <td class="nw"><button data-use-cam="${esc(c.index)}">Use index</button></td>
    </tr>`).join("");

  const cams = Object.values(state.cameras || {});
  const html = cams.map((c) => `
    <figure class="cam">
      <figcaption>
        <b>${esc(c.name)}</b> <span class="muted mono">#${esc(c.index)}</span>
        <span class="muted mono">${esc(c.fps_measured)} fps</span>
        ${c.error ? `<span class="sev-fail">${esc(c.error)}</span>` : ""}
      </figcaption>
      <img src="/api/video/${encodeURIComponent(c.name)}?fps=12" alt="${esc(c.name)}">
      <button data-close-cam="${esc(c.name)}">Close</button>
    </figure>`).join("");
  $("#cam-previews").innerHTML = html ||
    `<p class="hint">No cameras open. Scan above, then open one by index.</p>`;
  $("#teleop-cams").innerHTML = html;
  $("#teleop-cam-hint").hidden = cams.length > 0;
}

function travelBar(min, pos, max) {
  const pct = (v) => Math.min(100, Math.max(0, (v / 4095) * 100));
  // The numbers ride inside the bar rather than in their own column: six columns
  // made the table wider than the card and stretched the whole page.
  return `<div class="bar">
    <div class="rng" style="left:${pct(min)}%;width:${Math.max(0.6, pct(max) - pct(min))}%"></div>
    <div class="tgt" style="left:${pct(2047)}%"></div>
    <div class="pos" style="left:${pct(pos)}%"></div>
    <span class="lbl">${min} · ${pos} · ${max}</span>
  </div>`;
}

function renderSweep(calib) {
  const sweep = calib.sweep || {};
  const names = Object.keys(sweep);
  if (!names.length) {
    $("#sweep-table").innerHTML = `<p class="hint">No sweep data yet.</p>`;
    $("#sweep-hint").hidden = true;
    return;
  }
  const rows = names.map((n) => {
    const s = sweep[n];
    const cls = s.span >= 2200 ? "sev-ok" : s.span >= 2000 ? "sev-info" : "sev-warn";
    return `<tr>
      <td class="mono nw">${esc(n)}</td>
      <td class="bar-cell">${travelBar(s.min, s.pos, s.max)}</td>
      <td class="mono nw ${cls}">${s.span}</td>
      <td class="mono muted nw">${(s.span / 11.375).toFixed(0)}°</td>
      <td class="nw nowrap">
        <button data-mark="min" data-motor="${esc(n)}" title="Set min to the current position">min = now</button>
        <button data-mark="max" data-motor="${esc(n)}" title="Set max to the current position">max = now</button>
      </td>
    </tr>`;
  }).join("");
  $("#sweep-table").innerHTML = `<table class="tight"><tbody>${rows}</tbody></table>`;

  const flat = calib.flat_joints || [];
  $("#sweep-hint").hidden = flat.length === 0;
  if (flat.length) {
    $("#sweep-hint").innerHTML =
      `<span class="sev-warn">Not swept at all (min = max): ${esc(flat.join(", "))}</span>`;
  }
}

function renderDraft(calib) {
  const draft = calib.draft || {};
  const names = Object.keys(draft);
  if (!names.length) {
    $("#draft-table").innerHTML =
      `<p class="hint">No draft yet — run a sweep, or load from the file or the motors.</p>`;
    $("#apply-hint").textContent =
      "Writes are read back and verified — EEPROM silently rejects writes while Lock is set.";
    return;
  }
  const rows = names.map((n) => {
    const d = draft[n];
    const span = d.range_max - d.range_min;
    const field = (k) => `<input type="number" class="cell" data-field="${k}" data-motor="${esc(n)}" value="${esc(d[k])}">`;
    return `<tr>
      <td class="mono nw">${esc(n)}</td>
      <td>${field("range_min")}</td>
      <td>${field("range_max")}</td>
      <td>${field("homing_offset")}</td>
      <td class="mono muted nw">${span} <span class="muted">(${(span / 11.375).toFixed(0)}°)</span></td>
      <td class="mono muted nw">${((d.range_min + d.range_max) / 2).toFixed(0)}</td>
    </tr>`;
  }).join("");
  $("#draft-table").innerHTML = `<table class="tight">
    <thead><tr>
      <th>Joint</th><th>range_min</th><th>range_max</th>
      <th>homing_offset</th><th>Span</th><th>Mid</th>
    </tr></thead><tbody>${rows}</tbody></table>`;

  $("#apply-hint").innerHTML = calib.applied
    ? `<span class="sev-ok">Written.${calib.saved_path ? " File: " + esc(calib.saved_path) : ""}</span>`
    : "Writes are read back and verified — EEPROM silently rejects writes while Lock is set.";

  // Per-joint verdicts for this draft: span, wrap headroom, midpoint.
  renderFindings($("#calib-findings"), calib.findings);
}

const CALIB_STEPS = ["idle", "prepared", "homed", "sweeping", "review", "applied"];

function renderCalibSteps(phase) {
  const idx = CALIB_STEPS.indexOf(phase);
  $$("#calib-steps .step").forEach((el) => {
    const i = CALIB_STEPS.indexOf(el.dataset.step);
    el.className = "step" + (i === idx ? " active" : i < idx ? " done" : "");
  });
  const sweeping = phase === "sweeping";
  $("#btn-calib-sweep-start").disabled = sweeping || phase === "idle";
  $("#btn-calib-sweep-stop").disabled = !sweeping;
  $("#btn-calib-homing").disabled = sweeping;
}

function renderTeleop(t) {
  $("#tbl-teleop tbody").innerHTML = (t.rows || []).map((r) => `
    <tr>
      <td class="mono nw">${esc(r.joint)}</td>
      <td class="mono">${r.leader ?? "—"}</td>
      <td class="mono">${r.follower ?? "—"}</td>
      <td class="mono ${Math.abs(r.err ?? 0) > 5 ? "sev-warn" : ""}">${r.err ?? "—"}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted">Not running.</td></tr>`;

  const bits = [t.running ? "running" : "stopped"];
  if (t.running) bits.push(`${t.hz} Hz`, `${t.iter} frames`);
  if (t.fails) bits.push(`<span class="sev-warn">${t.fails} consecutive failures</span>`);
  if (t.error) bits.push(`<span class="sev-fail">${esc(t.error)}</span>`);
  $("#teleop-stats").innerHTML = bits.join(" · ");
}

/* Mirrors the backend's Rerun sink so the button can toggle it. */
let rerunState = { enabled: false, ready: false, error: null };
let lastRerunError = null;

function renderRerun(r) {
  rerunState = r || rerunState;
  const btn = $("#btn-rerun");
  if (rerunState.enabled && !rerunState.ready && !rerunState.error) btn.textContent = "Rerun view …";
  else if (rerunState.enabled) btn.textContent = "Rerun view ✓";
  else btn.textContent = "Rerun view";
  btn.classList.toggle("primary", !!rerunState.enabled);

  // Report a startup failure once, not on every 20 Hz tick.
  if (rerunState.error && rerunState.error !== lastRerunError) {
    lastRerunError = rerunState.error;
    flashError(`Rerun: ${rerunState.error}`);
  }
}

function renderLog(log) {
  const el = $("#log-box");
  el.innerHTML = (log || []).slice(-150).map((l) =>
    `<div class="${esc(l.level)}">${esc(new Date(l.ts * 1000).toLocaleTimeString())} ${esc(l.msg)}</div>`
  ).join("");
  el.scrollTop = el.scrollHeight;
}

function render(state) {
  fillPortSelects(state.ports);
  renderPorts(state.ports, state.probes);
  renderArm("follower", state.arms.follower);
  renderArm("leader", state.arms.leader);
  if (state.pair.length) renderFindings($("#pair-findings"), state.pair);
  else $("#pair-findings").innerHTML = `<p class="hint">Press Compare to run.</p>`;
  renderCameras(state);
  renderCalibSteps(state.calib.phase);
  renderSweep(state.calib);
  renderDraft(state.calib);
  renderTeleop(state.teleop);
  renderRerun(state.rerun);
  renderLog(state.log);
  $("#hdr-busy").textContent = state.busy ? `${state.busy}…` : "";
  if (state.last_error) flashError(state.last_error);
}

/* ------------------------------------------------------------------ events */

function showTab(name) {
  const known = $$(".tabs button").some((x) => x.dataset.tab === name);
  if (!known) return;
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".tabpage").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}

$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  const d = btn.dataset;
  if (d.probe) cmd("probe_port", { port: d.probe });
  else if (d.connect) {
    const role = d.connect;
    const armId = $(`#${role}-id`).value.trim();
    cfg.set(`port.${role}`, $(`#${role}-port`).value);
    cmd("set_arm_id", { role, arm_id: armId });
    cmd("connect_arm", { role, port: $(`#${role}-port`).value, arm_id: armId });
  }
  else if (d.disconnect) cmd("disconnect_arm", { role: d.disconnect });
  else if (d.health) cmd("health_check", { role: d.health });
  else if (d.torqueOff) cmd("set_torque", { role: d.torqueOff, on: false });
  else if (d.useCam !== undefined) $("#cam-index").value = d.useCam;
  else if (d.closeCam) cmd("close_camera", { name: d.closeCam });
  else if (d.mark) cmd("calib_mark", { role: $("#calib-role").value, motor: d.motor, which: d.mark });
  else if (d.draftFrom) cmd("calib_draft_from", { role: $("#calib-role").value, source: d.draftFrom });
});

document.addEventListener("change", (ev) => {
  const el = ev.target;
  if (el.dataset && el.dataset.field && el.dataset.motor) {
    cmd("calib_draft_set", {
      role: $("#calib-role").value, motor: el.dataset.motor,
      field: el.dataset.field, value: parseInt(el.value, 10) || 0,
    });
  } else if (el.id === "follower-port" || el.id === "leader-port") {
    cfg.set(`port.${el.id.replace("-port", "")}`, el.value);
  }
});

$("#btn-refresh-ports").onclick = () => cmd("refresh_ports");
$("#btn-compare").onclick = () => cmd("compare_arms");
$("#btn-scan-cams").onclick = () => cmd("scan_cameras");
$("#btn-open-cam").onclick = () => {
  const name = $("#cam-name").value.trim();
  if (!name) return flashError("Give the camera a name first.");
  cmd("open_camera", {
    name, index: $("#cam-index").value.trim(),
    width: parseInt($("#cam-w").value, 10), height: parseInt($("#cam-h").value, 10),
    fps: parseInt($("#cam-fps").value, 10),
  });
};
$("#btn-panic").onclick = () => cmd("release_all");
$("#btn-calib-prepare").onclick = () => cmd("calib_prepare", { role: $("#calib-role").value });
$("#btn-calib-homing").onclick = () => cmd("calib_set_homing", { role: $("#calib-role").value });
$("#btn-calib-sweep-start").onclick = () => cmd("calib_sweep_start", { role: $("#calib-role").value });
$("#btn-calib-sweep-stop").onclick = () => cmd("calib_sweep_stop", { role: $("#calib-role").value });
$("#btn-calib-apply").onclick = () => {
  if (!confirm("Write this draft into the motor registers and overwrite the calibration file. Continue?")) return;
  cmd("calib_apply", { role: $("#calib-role").value, write_motors: true, write_json: true });
};
$("#btn-rerun").onclick = () => cmd("set_rerun", { enabled: !rerunState.enabled });

/* Opens a named window, so clicking again reuses it instead of stacking tabs. */
$("#btn-operator").onclick = () => {
  const w = window.open("/operator", "so101-operator", "width=1280,height=800");
  if (w) w.focus();
  else flashError("The browser blocked the popup — allow popups for this page.");
};
$("#btn-teleop-start").onclick = () => cmd("teleop_start");
$("#btn-teleop-stop").onclick = () => cmd("teleop_stop");
$("#btn-teleop-panic").onclick = async () => {
  await fetch("/api/estop", { method: "POST" });
  cmd("release_all");
};

/* ------------------------------------------------------------------ boot */

function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (m) => render(JSON.parse(m.data));
  es.onerror = () => { $("#hdr-busy").textContent = "reconnecting…"; };
}

// ?tab=health|calib|teleop so a tab can be bookmarked.
showTab(new URLSearchParams(location.search).get("tab") || "health");
cmd("refresh_ports");
fetch("/api/state").then((r) => r.json()).then(render);
connectSSE();
