// Lamplighter dashboard: the heads-up display around the street, and the incident case below it.
// All server data is placed with textContent; nothing is parsed as HTML.
import { Street } from "./street.js";

const STEPS = [["DETECTED", "Diagnose"], ["AWAITING_APPROVAL", "Approve"], ["PREFLIGHT", "Rehearse"], ["GATE", "Hold"], ["ACTING", "Act"], ["VERIFYING", "Prove"]];
const CLOSED = { RESOLVED: "Fixed and proven", SELF_RESOLVED: "Cleared by itself", ESCALATED: "Handed to a human" };
const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);
let selected = null, token = "", street = null;
const lastStates = new Map();

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
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "-");
const sentence = (text) => text.charAt(0).toUpperCase() + text.slice(1);
async function api(path, options) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) throw new Error(response.status + " " + (await response.text()).slice(0, 200));
  return response.json();
}
function describePlan(plan) {
  if (!plan) return "none";
  if (plan.kind === "rollback_release") return "roll " + plan.service + " back from " + plan.from + " to " + plan.to;
  if (plan.kind === "revert_config") return "set " + plan.key + " on " + plan.service + " back from " + plan.from + " to " + plan.to;
  return plan.kind + " " + plan.service;
}

// ---------- theme ----------
function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  $("theme").textContent = name === "dark" ? "Light mode" : "Dark mode";
  $("theme").setAttribute("aria-pressed", String(name === "light"));
  if (street) street.setTheme(name);
  try { localStorage.setItem("lamplighter-theme", name); } catch (error) { /* storage unavailable: the choice lasts for this visit */ }
}
function initialTheme() {
  const asked = new URLSearchParams(location.search).get("theme");
  if (asked === "light" || asked === "dark") return asked;
  try { const stored = localStorage.getItem("lamplighter-theme"); if (stored === "light" || stored === "dark") return stored; } catch (error) { /* ignore */ }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

// ---------- heads-up display ----------
function headline(status, incident) {
  const edgeName = Object.keys(status.services)[0], edge = edgeName ? status.services[edgeName] : null;
  let title = "Every lamp is lit", mood = "calm";
  const fresh = incident && (!incident.closed_at || status.now - incident.closed_at < 25);
  if (fresh) {
    const data = incident.data, root = (data.diagnosis || {}).root, where = root ? root.service : incident.service, outcome = data.outcome || {};
    mood = "work";
    if (incident.state === "DETECTED") { title = "The street is dimming. Finding out why"; mood = "alarm"; }
    else if (incident.state === "AWAITING_APPROVAL") title = "Ready to " + describePlan(data.plan) + ". Waiting for your go-ahead";
    else if (incident.state === "PREFLIGHT") title = "Rehearsing the fix for " + where + " in a sandbox";
    else if (incident.state === "GATE") title = "Fix rehearsed. Checking " + where + " still needs it";
    else if (incident.state === "ACTING") title = "On the way to " + where;
    else if (incident.state === "VERIFYING") title = "Fix applied. Watching live traffic for proof";
    else if (incident.state === "REVERTING") { title = "That did not work. Undoing it"; mood = "alarm"; }
    else if (incident.state === "RESOLVED") { title = "Fixed and proven in " + Math.round(outcome.time_to_close_s) + " seconds"; mood = "calm"; }
    else if (incident.state === "SELF_RESOLVED") { title = "It cleared by itself. Nothing was touched"; mood = "calm"; }
    else if (incident.state === "ESCALATED") { title = "This one needs a human"; mood = "alarm"; }
  } else if (edge && edge.firing) { title = "The street is dimming"; mood = "alarm"; }
  $("stage").dataset.mood = mood;
  if (document.documentElement.dataset.mood !== mood) {     // the logo's flame and the tab icon follow the street
    document.documentElement.dataset.mood = mood;
    $("icon").setAttribute("href", "/static/icon-" + mood + ".svg");
  }
  document.title = mood === "calm" && !fresh ? "Lamplighter: every lamp is lit" : title + " - Lamplighter";
  $("headline").textContent = title;
  const watching = "Watching " + ((status.inputs || {}).services || 0) + " services in the " + ((status.inputs || {}).environment || "system") + ". When customers start failing, it finds the cause, rehearses a fix, applies it and proves it.";
  $("subline").textContent = fresh && incident.state === "ESCALATED" ? sentence((incident.data.outcome || {}).reason || "") : fresh && incident.data.plan && !incident.closed_at ? "Plan: " + describePlan(incident.data.plan) + "." : fresh ? "" : watching;
}

function vitals(status) {
  const edgeName = Object.keys(status.services)[0], edge = edgeName ? status.services[edgeName] : null;
  if (!edge) return;
  const recent = edge.series.slice(-5).reduce((a, b) => [a[0] + b[0], a[1] + b[1]], [0, 0]);
  const share = recent[0] ? (100 * recent[1]) / recent[0] : 0;
  $("gauge").style.setProperty("--share", Math.min(100, share).toFixed(1));
  $("gauge").dataset.level = share >= 5 ? "bad" : "good";
  $("gauge-value").textContent = (share >= 10 ? share.toFixed(0) : share.toFixed(1)) + "%";
  $("rate").textContent = (recent[0] / (5 * edge.bucket_s)).toFixed(0);
  $("promise").textContent = (edge.slo.target * 100) + "%";
  $("alert").textContent = edge.firing ? "Burning " + Math.round(edge.burn_short) + " times too fast" : "Within the promise";
  $("alert").dataset.level = edge.firing ? "bad" : "good";
  const strip = $("strip"), peak = Math.max(1, ...edge.series.map((b) => b[0]));
  while (strip.children.length < edge.series.length) strip.append(h("i", {}, h("b", {})));
  edge.series.forEach(([total, bad], index) => {
    const bar = strip.children[index];
    bar.style.height = Math.max(4, (100 * total) / peak) + "%";
    bar.firstChild.style.height = (total ? (100 * bad) / total : 0) + "%";
  });
  $("badges").replaceChildren(...[
    h("span", { class: "pill" + (status.mode === "auto" ? " on" : "") }, status.mode === "auto" ? "Fixes on its own" : status.mode === "propose" ? "Asks before fixing" : "Watching only"),
    status.kill_switch ? h("span", { class: "pill warn" }, "Kill switch on") : null,
    h("span", { class: "pill" }, status.model ? "Model on" : "Model off")].filter(Boolean));
}

// ---------- what goes in: telemetry, changes, policy, and (in the demo) faults you inject ----------
const ACTIONS = { rollback_release: "Roll back a release", revert_config: "Revert a setting", restart: "Restart a service" };
const TIERS = { auto: "on its own", approval: "after approval", deny: "never" };
function describeChange(change) {
  if (change.kind === "deploy") return change.service + " deployed " + change.from + " to " + change.to;
  if (change.kind === "config") return change.service + " setting " + change.key + " changed " + change.from + " to " + change.to;
  return change.service + " restarted";
}
function inputs(status) {
  const data = status.inputs;
  if (!data) return;
  $("in-telemetry").textContent = "About " + data.records_per_s + " records a second (requests and calls) from " + data.services + " services, plus every deploy and setting change.";
  const changes = data.changes.slice().reverse();
  $("in-changes").replaceChildren(...(changes.length ? changes.map((change) => {
    const own = change.actor === "lamplighter";
    return h("li", {}, h("time", {}, clock(change.ts)), h("span", {}, describeChange(change) + ", by ", h("span", { class: own ? "auto" : "" }, own ? "Lamplighter" : change.actor)));
  }) : [h("li", { class: "quiet" }, "No deploys or setting changes yet.")]));
  $("in-policy").replaceChildren(...Object.entries(data.policy).map(([kind, tier]) =>
    h("li", { class: tier }, (ACTIONS[kind] || kind) + ": ", h("b", {}, TIERS[tier] || tier))));
}
let labReady = false, busy = false;
async function sendFault(name, label) {
  try {
    const response = await fetch("/v1/demo/scenarios/" + name, { method: "POST", headers: { "X-Lamplighter-Demo": "1" } });
    $("lab-note").textContent = response.ok ? "Sent: " + label.toLowerCase() + ". Watch the street, then read the case below." : response.status === 409 ? "Wait for the open incident to close first." : "That did not work (" + response.status + ").";
  } catch (error) { $("lab-note").textContent = "Could not reach the demo."; }
}
async function loadLab() {
  try {
    const response = await fetch("/v1/demo/scenarios");
    if (!response.ok) return;                          // not the demo: the panel stays hidden
    const list = (await response.json()).scenarios.filter((s) => s.name !== "reset");
    $("scenarios").replaceChildren(...list.map((s) => h("button", { class: "card", type: "button", onclick: () => sendFault(s.name, s.label) },
      h("b", {}, s.label), h("span", {}, s.what), h("em", {}, "A correct response: " + s.expect.charAt(0).toLowerCase() + s.expect.slice(1)))));
    $("reset").addEventListener("click", () => sendFault("reset", "Reset the shop"));
    $("lab").hidden = false; labReady = true;
  } catch (error) { /* no demo panel */ }
}
function lockLab(open) {
  if (!labReady || busy === open) return;
  busy = open;
  for (const card of document.querySelectorAll("#scenarios .card")) card.disabled = open;
}

// ---------- incidents ----------
function renderList(list) {
  const root = $("incidents");
  if (!list.length) { root.replaceChildren(h("p", { class: "quiet" }, "None so far.")); return; }
  if (!selected) selected = list[0].id;
  root.replaceChildren(...list.map((incident) =>
    h("button", { class: "inc" + (incident.id === selected ? " sel" : "") + (incident.state === "RESOLVED" || incident.state === "SELF_RESOLVED" ? " resolved" : incident.state === "ESCALATED" ? " escalated" : " open"),
      type: "button", onclick: () => { selected = incident.id; refresh(); } },
      h("b", {}, sentence(incident.summary || "working out what happened to " + incident.service)),
      h("span", {}, clock(incident.opened_at) + ", " + (CLOSED[incident.state] || "in progress").toLowerCase()))));
}

function track(incident) {
  const visited = new Set(incident.data.timeline.map((t) => t.from)), closed = CLOSED[incident.state];
  const steps = STEPS.filter(([state]) => (state !== "AWAITING_APPROVAL" && state !== "GATE") || visited.has(state) || incident.state === state);
  const items = steps.map(([state, label]) => h("li", { class: incident.state === state ? "now" : visited.has(state) ? "done" : "" }, label));
  if (visited.has("REVERTING") || incident.state === "REVERTING") items.push(h("li", { class: incident.state === "REVERTING" ? "now" : "bad" }, "Undo"));
  items.push(h("li", { class: closed ? (incident.state === "ESCALATED" ? "bad" : "done") : "" }, closed || "Done"));
  return h("ol", { class: "track" }, items);
}

function hypothesis(hyp, acceptedId) {
  const accepted = hyp.id === acceptedId;
  return h("section", { class: "hyp" + (accepted ? " accepted" : "") },
    h("header", {}, h("b", {}, sentence(hyp.summary)), h("span", { class: "verdict" + (accepted ? " yes" : "") }, accepted ? "Accepted" : hyp.accepted ? "Also passes" : "Ruled out")),
    h("ul", { class: "checks" }, hyp.checks.map((check) =>
      h("li", { class: check.passed ? "pass" : "fail" },
        h("b", {}, sentence(check.name) + (check.required ? "" : " (supporting)")),
        h("span", {}, sentence(check.detail)), h("code", {}, check.evidence.join(" "))))));
}

function proofs(data) {
  const items = [], pre = data.preflight;
  if (pre) {
    if (pre.control) items.push(h("li", {}, h("b", {}, "Control"), "In a sandbox copy of the broken state, " + pre.control.failing_reproduced + " of " + pre.control.failing_total + " failing requests failed again."));
    if (pre.fix) items.push(h("li", { class: pre.fix.passed ? "" : "no" }, h("b", {}, "Fix"), "With the fix applied, " + pre.fix.failing.passed + " of " + pre.fix.failing.total + " were served, and " + pre.fix.golden.passed + " of " + pre.fix.golden.total + " healthy requests came back identical."));
    if (!pre.control && !pre.fix) items.push(h("li", { class: pre.passed ? "" : "no" }, h("b", {}, "Before acting"), sentence(pre.note || "")));
  }
  for (const check of (data.verify || {}).checks || []) {
    items.push(h("li", { class: check.healthy ? "" : "no" }, h("b", {}, "Live, " + clock(check.checked_at)),
      check.window.bad + " bad of " + check.window.total + " requests, " + check.recurrence + " repeats of the error."));
  }
  return items.length ? h("ul", { class: "proofs" }, items) : h("p", { class: "quiet" }, "Nothing has been changed, so there is nothing to prove yet.");
}

async function decide(incident, decision) {
  try {
    await api("/v1/incidents/" + incident.id + "/decision", { method: "POST", body: JSON.stringify({ decision, plan_hash: incident.data.plan_hash, actor: "dashboard" }) });
  } catch (error) { console.error("decision failed", error.message); }
  refresh();
}

function renderDetail(incident) {
  const data = incident.data, diagnosis = data.diagnosis || { hypotheses: [] }, outcome = data.outcome || {};
  const accepted = diagnosis.hypotheses.find((hyp) => hyp.id === diagnosis.accepted);
  const evidence = (data.bundle || {}).items || [];
  const nodes = [
    h("p", { class: "meta" }, incident.id + ", first failure at " + clock((data.bundle || {}).onset)),
    h("h2", { class: "lead" }, sentence(accepted ? accepted.summary : "working out what happened to " + incident.service)),
    track(incident),
    h("dl", { class: "facts" },
      h("div", {}, h("dt", {}, "Noticed after"), h("dd", {}, outcome.time_to_detect_s != null ? outcome.time_to_detect_s.toFixed(1) + " s" : "...")),
      h("div", {}, h("dt", {}, "Closed after"), h("dd", {}, outcome.time_to_close_s != null ? outcome.time_to_close_s.toFixed(1) + " s" : "open")),
      h("div", {}, h("dt", {}, "Action"), h("dd", {}, data.plan ? data.plan.kind.replace("_", " ") : "none")),
      h("div", {}, h("dt", {}, "Decided by"), h("dd", {}, data.policy ? (data.policy.verdict === "auto" ? "policy" : data.policy.verdict === "approval" ? "a human" : "nobody") : "..."))),
  ];
  if (outcome.reason) nodes.push(h("p", { class: "outcome" }, sentence(outcome.reason) + (data.plan ? ". Plan: " + describePlan(data.plan) + "." : ".")));
  if (incident.state === "AWAITING_APPROVAL") {
    nodes.push(h("p", { class: "actions" }, h("button", { class: "go", type: "button", onclick: () => decide(incident, "approved") }, "Approve: " + describePlan(data.plan)),
      h("button", { class: "no", type: "button", onclick: () => decide(incident, "rejected") }, "Reject")));
  }
  const why = h("div", {}, h("h3", {}, "Why it thinks so"), ...diagnosis.hypotheses.map((hyp) => hypothesis(hyp, diagnosis.accepted)));
  if (!diagnosis.hypotheses.length) why.append(h("p", { class: "quiet" }, "Collecting evidence."));
  const EVENTS = { diagnosed: "Diagnosed; plan made", approval_requested: "Asked a human to approve", approved: "Approved", preflight_passed: "Fix rehearsed in a sandbox",
    gate_passed: "Still failing, plan still valid", action_done: "Fix applied", proof_failed: "Proof failed; undoing", closed: CLOSED[incident.state] || "Closed" };
  const side = h("div", {}, h("h3", {}, "What happened, step by step"),
    h("ul", { class: "steps" }, [h("li", {}, h("time", {}, clock((data.bundle || {}).onset)), "First customer request failed"),
      h("li", {}, h("time", {}, clock(incident.opened_at)), "Lamplighter noticed")].concat(data.timeline.map((step) =>
      h("li", {}, h("time", {}, clock(step.ts)), EVENTS[step.event] || step.event)))),
    h("h3", {}, "Proof"), proofs(data));
  if (data.fixforward) {
    const brief = data.fixforward, frame = brief.culprit.frame, patch = brief.patch;
    side.append(h("h3", {}, "The line that broke"),
      h("p", {}, frame ? [brief.file + ", line " + frame.line + ", in " + frame.func + ". ", brief.culprit.in_changed_code ? "This release added it." : "This release did not change it."] : "No stack frame was captured."),
      h("pre", {}, patch.diff || (brief.culprit.hunk ? brief.culprit.hunk.text : brief.culprit.diff)),
      h("p", { class: "quiet" }, "Suggested patch from the model: " + patch.status.replace("_", " ") + ". " + sentence(patch.reason || "")));
  }
  if (data.narrative && (data.narrative.narrative.length || data.narrative.next_steps.length)) {
    side.append(h("h3", {}, "In plain words"), h("p", { class: "quiet" }, "Written by a model. " + (data.narrative.dropped_ungrounded === 1 ? "1 unsupported sentence was" : data.narrative.dropped_ungrounded + " unsupported sentences were") + " removed."),
      h("ul", { class: "proofs" }, data.narrative.narrative.slice(0, 8).concat(data.narrative.next_steps.slice(0, 4)).map((s) => h("li", {}, s.text + " ", h("code", {}, s.evidence.join(" "))))));
  }
  nodes.push(h("div", { class: "split" }, why, side));
  const open = document.querySelector("#detail details") ? document.querySelector("#detail details").open : false;
  const drawer = h("details", {}, h("summary", {}, "All " + evidence.length + " pieces of evidence"),
    h("ul", { class: "evidence" }, evidence.map((item) => h("li", {}, h("code", {}, item.id), item.summary))));
  drawer.open = open;
  nodes.push(drawer, h("p", { class: "foot" }, incident.audit.length + " audit entries, chain head " + (incident.audit.length ? incident.audit[incident.audit.length - 1].hash.slice(0, 16) : "-")));
  $("detail").replaceChildren(...nodes);
}

// ---------- loop ----------
async function refresh() {
  try {
    const [status, list] = await Promise.all([api("/v1/status"), api("/v1/incidents?limit=20")]);
    vitals(status);
    inputs(status);
    lockLab(list.incidents.some((incident) => !incident.closed_at));
    renderList(list.incidents);
    const newest = list.incidents[0] ? await api("/v1/incidents/" + list.incidents[0].id) : null;
    let root = null;
    if (newest) {
      const before = lastStates.get(newest.id), plan = newest.data.plan;
      const where = ((newest.data.diagnosis || {}).root || {}).service || (plan || {}).service;
      if (!newest.closed_at || status.now - newest.closed_at < 6) root = where;
      if (street) {
        if ((newest.state === "ACTING" || newest.state === "VERIFYING") && plan) street.sendTo(plan.service); else street.recall();
        if (before && before !== newest.state && newest.state === "RESOLVED" && where) street.ring(where, street.mint());
      }
      lastStates.set(newest.id, newest.state);
    }
    if (street && status.topology) street.update(status.topology, root);
    headline(status, newest);
    if (selected) renderDetail(newest && newest.id === selected ? newest : await api("/v1/incidents/" + selected));
  } catch (error) {
    $("headline").textContent = "Cannot reach Lamplighter";
    $("subline").textContent = error.message;
  }
}

try {
  street = new Street($("scene"), $("tags"), $("leaders"), calm);
} catch (error) {
  $("stage").classList.add("flat");          // no WebGL: the numbers and the case below still work
  console.error("3D view unavailable:", error.message);
}
applyTheme(initialTheme());
$("theme").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("auth").addEventListener("submit", (event) => event.preventDefault());
$("token").addEventListener("input", (event) => { token = event.target.value.trim(); });
loadLab();
refresh();
setInterval(refresh, 1000);
