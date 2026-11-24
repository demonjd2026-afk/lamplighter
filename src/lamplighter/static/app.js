"use strict";
// Dashboard. All server data is placed with textContent; nothing is parsed as HTML.
const PIPELINE = ["DETECTED", "AWAITING_APPROVAL", "PREFLIGHT", "GATE", "ACTING", "VERIFYING"];
const TERMINAL = { RESOLVED: "ok", SELF_RESOLVED: "info", ESCALATED: "warn" };
let selected = null;
let token = "";

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "-";
}

async function api(path, options) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) throw new Error(response.status + " " + (await response.text()).slice(0, 200));
  return response.json();
}

function badge(text, kind) {
  return h("span", { class: "badge " + (kind || "") }, text);
}

function drawSeries(canvas, series) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const peak = Math.max(1, ...series.map((b) => b[0]));
  const barWidth = width / series.length;
  series.forEach(([total, bad], index) => {
    const full = (total / peak) * (height - 4);
    const badHeight = total ? (bad / total) * full : 0;
    ctx.fillStyle = "#2b5f48";
    ctx.fillRect(index * barWidth, height - full, Math.max(1, barWidth - 1), full - badHeight);
    ctx.fillStyle = "#ff5d6c";
    ctx.fillRect(index * barWidth, height - badHeight, Math.max(1, barWidth - 1), badHeight);
  });
}

function renderStatus(status) {
  const badges = document.getElementById("badges");
  badges.replaceChildren(
    badge("mode: " + status.mode, status.mode === "auto" ? "ok" : "info"),
    badge(status.kill_switch ? "kill switch ON" : "kill switch off", status.kill_switch ? "bad" : ""),
    badge(status.model ? "model: on" : "model: off", status.model ? "info" : ""));
  const root = document.getElementById("services");
  const nodes = [];
  for (const [name, service] of Object.entries(status.services)) {
    const canvas = h("canvas", {});
    const recent = service.series.slice(-5).reduce((acc, b) => [acc[0] + b[0], acc[1] + b[1]], [0, 0]);
    const share = recent[0] ? ((100 * recent[1]) / recent[0]).toFixed(1) + "% failing" : "no traffic";
    nodes.push(h("div", {},
      h("div", { class: "svc" }, h("strong", {}, name),
        badge(service.firing ? "ALERT burn " + service.burn_short : "within SLO", service.firing ? "bad" : "ok")),
      canvas,
      h("p", { class: "muted" }, "SLO " + (service.slo.target * 100) + "% - last 5 buckets: " + share)));
    requestAnimationFrame(() => drawSeries(canvas, service.series));
  }
  root.replaceChildren(...nodes);
}

function renderList(list) {
  const root = document.getElementById("incidents");
  if (!list.length) { root.replaceChildren(h("p", { class: "muted" }, "No incidents yet.")); return; }
  if (!selected) selected = list[0].id;
  root.replaceChildren(...list.map((incident) =>
    h("div", { class: "inc" + (incident.id === selected ? " sel" : ""), onclick: () => { selected = incident.id; refresh(); } },
      h("div", { class: "top" }, h("span", { class: "id" }, incident.id),
        badge(incident.state, TERMINAL[incident.state] || "bad")),
      h("p", {}, incident.summary || incident.reason || "diagnosing " + incident.service + "..."))));
}

function pipeline(incident) {
  const visited = new Set(incident.data.timeline.map((t) => t.from));
  const steps = PIPELINE.filter((s) => s !== "AWAITING_APPROVAL" || visited.has(s) || incident.state === s);
  const nodes = steps.map((step) =>
    h("span", { class: "step" + (incident.state === step ? " now" : visited.has(step) ? " done" : "") }, step));
  if (TERMINAL[incident.state]) nodes.push(h("span", { class: "step now" }, incident.state));
  if (visited.has("REVERTING") || incident.state === "REVERTING") nodes.splice(-1, 0, h("span", { class: "step done" }, "REVERTING"));
  return h("div", { class: "pipeline" }, nodes);
}

function hypothesis(hyp, acceptedId) {
  const accepted = hyp.id === acceptedId;
  return h("div", { class: "hyp" + (accepted ? " accepted" : "") },
    h("div", { class: "head" }, h("strong", {}, hyp.id + ": " + hyp.summary),
      badge(accepted ? "ACCEPTED" : hyp.accepted ? "passes" : "rejected", accepted ? "ok" : hyp.accepted ? "info" : "")),
    h("table", {}, h("tbody", {}, hyp.checks.map((check) =>
      h("tr", {}, h("td", { class: check.passed ? "pass" : "fail" }, check.passed ? "pass" : "FAIL"),
        h("td", {}, check.name + (check.required ? "" : " (supporting)")),
        h("td", {}, check.detail), h("td", { class: "mono" }, check.evidence.join(" ")))))));
}

function proof(data) {
  const items = [];
  const pre = data.preflight;
  if (pre) {
    if (pre.control) items.push(h("li", {}, "Control, sandbox in the current state: " + pre.control.failing_reproduced +
      " of " + pre.control.failing_total + " failing requests reproduced the failure."));
    if (pre.fix) items.push(h("li", {}, "Fix, sandbox with the fix applied: " + pre.fix.failing.passed + " of " +
      pre.fix.failing.total + " failing requests served; " + pre.fix.golden.passed + " of " + pre.fix.golden.total +
      " healthy requests reproduced exactly."));
    items.push(h("li", {}, h("strong", {}, pre.passed ? "Before acting: pass. " : "Before acting: FAIL. "), pre.note || ""));
  }
  for (const check of (data.verify || {}).checks || []) {
    items.push(h("li", {}, "Live traffic at " + clock(check.checked_at) + ": " + check.window.bad + " bad of " +
      check.window.total + " requests, " + check.recurrence + " recurrences: ",
      h("strong", {}, check.healthy ? "healthy" : "not healthy")));
  }
  return items.length ? h("ul", { class: "proof" }, items) : h("p", { class: "muted" }, "Nothing to prove yet.");
}

async function decide(incident, decision) {
  try {
    await api("/v1/incidents/" + incident.id + "/decision", { method: "POST",
      body: JSON.stringify({ decision, plan_hash: incident.data.plan_hash, actor: "dashboard" }) });
  } catch (error) { console.error("decision failed", error.message); }
  refresh();
}

function describePlan(plan) {
  if (!plan) return "none";
  if (plan.kind === "rollback_release") return "roll " + plan.service + " back from " + plan.from + " to " + plan.to;
  if (plan.kind === "revert_config") return "set " + plan.key + " on " + plan.service + " back from " + plan.from + " to " + plan.to;
  return plan.kind + " " + plan.service;
}

function renderDetail(incident) {
  const data = incident.data, diagnosis = data.diagnosis || { hypotheses: [] }, outcome = data.outcome || {};
  const nodes = [
    h("h2", {}, "Incident " + incident.id), pipeline(incident),
    h("div", { class: "kv" },
      h("div", {}, "Service"), h("div", {}, incident.service),
      h("div", {}, "First failure"), h("div", {}, clock((data.bundle || {}).onset)),
      h("div", {}, "Detected"), h("div", {}, clock(incident.opened_at) + (outcome.time_to_detect_s != null ? "  (" + outcome.time_to_detect_s + " s after first failure)" : "")),
      h("div", {}, "Closed"), h("div", {}, incident.closed_at ? clock(incident.closed_at) + "  (" + outcome.time_to_close_s + " s after first failure)" : "open"),
      h("div", {}, "Outcome"), h("div", {}, outcome.reason || "in progress"),
      h("div", {}, "Plan"), h("div", {}, describePlan(data.plan)),
      h("div", {}, "Policy"), h("div", {}, data.policy ? data.policy.verdict + ": " + data.policy.reasons.join("; ") : "-"))];
  if (incident.state === "AWAITING_APPROVAL") {
    nodes.push(h("p", {}, h("button", { onclick: () => decide(incident, "approved") }, "Approve plan"),
      h("button", { class: "ghost", onclick: () => decide(incident, "rejected") }, "Reject")));
  }
  nodes.push(h("h3", {}, "Diagnosis"), h("p", { class: "muted" }, diagnosis.reason || "collecting evidence"),
    ...diagnosis.hypotheses.map((hyp) => hypothesis(hyp, diagnosis.accepted)));
  nodes.push(h("h3", {}, "Proof"), proof(data));
  if (data.fixforward) {
    const brief = data.fixforward, frame = brief.culprit.frame;
    nodes.push(h("h3", {}, "Fix-forward brief"),
      h("p", {}, frame ? "Failing line " + brief.file + ":" + frame.line + " in " + frame.func + " (" +
        (brief.culprit.in_changed_code ? "inside" : "outside") + " the code this release changed): " : "",
        frame ? h("code", {}, brief.culprit.line_text || "") : null),
      h("p", { class: "muted" }, "Model patch: " + brief.patch.status + " - " + (brief.patch.reason || "")),
      h("pre", {}, brief.patch.diff || (brief.culprit.hunk ? "lines " + brief.culprit.hunk.start + "-" + brief.culprit.hunk.end +
        " of " + brief.bad_release + "/" + brief.file + " (added or changed by this release):\n\n" + brief.culprit.hunk.text : brief.culprit.diff)));
  }
  if (data.narrative && (data.narrative.narrative.length || data.narrative.next_steps.length)) {
    nodes.push(h("h3", {}, "Narrative (language model, filtered: " + data.narrative.dropped_ungrounded + " ungrounded sentences removed)"),
      h("ul", {}, data.narrative.narrative.concat(data.narrative.next_steps).map((s) =>
        h("li", {}, s.text + " ", h("span", { class: "mono muted" }, "[" + s.evidence.join(", ") + "]")))));
  }
  nodes.push(h("h3", {}, "Evidence"), h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Id"), h("th", {}, "Kind"), h("th", {}, "Summary"))),
    h("tbody", {}, ((data.bundle || {}).items || []).map((item) =>
      h("tr", {}, h("td", { class: "mono" }, item.id), h("td", {}, item.kind), h("td", {}, item.summary))))));
  nodes.push(h("h3", {}, "Timeline"), h("table", {}, h("tbody", {}, data.timeline.map((step) =>
    h("tr", {}, h("td", { class: "mono" }, clock(step.ts)), h("td", {}, step.from + " -> " + step.to), h("td", {}, step.event))))));
  nodes.push(h("p", { class: "muted mono" }, "audit entries: " + incident.audit.length + ", last hash " +
    (incident.audit.length ? incident.audit[incident.audit.length - 1].hash.slice(0, 16) : "-")));
  document.getElementById("detail").replaceChildren(...nodes);
}

async function refresh() {
  try {
    const [status, list] = await Promise.all([api("/v1/status"), api("/v1/incidents?limit=20")]);
    renderStatus(status);
    renderList(list.incidents);
    if (selected) renderDetail(await api("/v1/incidents/" + selected));
  } catch (error) {
    document.getElementById("services").replaceChildren(h("p", { class: "muted" }, "Cannot load: " + error.message));
  }
}

document.getElementById("auth").addEventListener("submit", (event) => event.preventDefault());
document.getElementById("token").addEventListener("input", (event) => { token = event.target.value.trim(); });
refresh();
setInterval(refresh, 1500);
