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
    $("lab-note").textContent = response.ok ? (name === "reset" ? "The shop is being reset." : "Sent: " + label.toLowerCase() + ". Watch the street; the incident console below fills in as it works.") : response.status === 409 ? "Wait for the open incident to close first." : "That did not work (" + response.status + ").";
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

// ---------- the incident console: one framed panel per question a responder asks ----------
function panel(title, tag, tone, ...body) {
  return h("section", { class: "panel" + (tone ? " " + tone : "") },
    h("header", {}, h("h3", {}, title), tag ? h("span", { class: "chip " + (tone || "") }, tag) : null), ...body);
}
function rows(pairs) {
  return h("dl", { class: "rows" }, pairs.filter(Boolean).map(([key, value]) => h("div", {}, h("dt", {}, key), h("dd", {}, value))));
}
function meter(label, done, total, tone) {
  const bar = h("i", {}); bar.style.width = (total ? (100 * done) / total : 0) + "%";
  return h("div", { class: "meter " + tone }, h("div", {}, h("span", {}, label), h("b", {}, done + " / " + total)), h("div", { class: "rail-bar" }, bar));
}
function checks(hyp) {
  return h("ul", { class: "checks" }, hyp.checks.map((check) =>
    h("li", { class: check.passed ? "pass" : "fail" },
      h("b", {}, sentence(check.name) + (check.required ? "" : " (supporting)")),
      h("span", {}, sentence(check.detail)), h("code", {}, check.evidence.join(" ")))));
}

async function decide(incident, decision) {
  try {
    await api("/v1/incidents/" + incident.id + "/decision", { method: "POST", body: JSON.stringify({ decision, plan_hash: incident.data.plan_hash, actor: "dashboard" }) });
  } catch (error) { console.error("decision failed", error.message); }
  refresh();
}

function renderDetail(incident, now) {
  const data = incident.data, diagnosis = data.diagnosis || { hypotheses: [] }, outcome = data.outcome || {};
  const accepted = diagnosis.hypotheses.find((hyp) => hyp.id === diagnosis.accepted);
  const others = diagnosis.hypotheses.filter((hyp) => hyp !== accepted);
  const bundle = data.bundle || {}, evidence = bundle.items || [], root = diagnosis.root, alert = data.alert || {};
  const burn = evidence.find((item) => item.kind === "slo_burn"), pre = data.preflight, plan = data.plan;
  const closed = CLOSED[incident.state], tone = incident.state === "ESCALATED" ? "bad" : closed ? "good" : "live";
  const elapsed = incident.closed_at ? outcome.time_to_close_s : bundle.onset ? now - bundle.onset : null;
  const keep = new Set([...document.querySelectorAll("#detail details[open]")].map((d) => d.dataset.key));
  const drawer = (key, label, ...body) => { const d = h("details", { "data-key": key }, h("summary", {}, label), ...body); d.open = keep.has(key); return d; };

  const head = h("header", { class: "console-head " + tone },
    h("span", { class: "chip " + tone }, closed || "In progress"),
    h("h2", {}, sentence(accepted ? accepted.summary : "working out what happened to " + incident.service)),
    h("div", { class: "console-meta" }, h("span", {}, incident.id), h("b", {}, elapsed != null ? elapsed.toFixed(1) + " s" : "...")));

  const tiles = h("dl", { class: "facts" },
    h("div", {}, h("dt", {}, "Noticed after"), h("dd", {}, outcome.time_to_detect_s != null ? outcome.time_to_detect_s.toFixed(1) + " s" : bundle.onset ? (incident.opened_at - bundle.onset).toFixed(1) + " s" : "...")),
    h("div", {}, h("dt", {}, closed ? "Closed after" : "Elapsed"), h("dd", {}, elapsed != null ? elapsed.toFixed(1) + " s" : "...")),
    h("div", {}, h("dt", {}, "Customer requests failed"), h("dd", {}, burn ? burn.data.bad + " of " + burn.data.requests : "...")),
    h("div", {}, h("dt", {}, "Action taken"), h("dd", {}, (incident.actions || []).some((a) => a.status === "DONE") && plan ? plan.kind.replace("_", " ") : "none")));

  const trigger = panel("1. Trigger", alert.burn_short ? "burn x" + Math.round(alert.burn_short) : null, "bad",
    h("p", { class: "say" }, burn ? sentence(burn.summary) + "." : "An alert opened this incident."),
    rows([["First failure", clock(bundle.onset)], ["Noticed", clock(incident.opened_at)],
      ["Promise", alert.slo ? (alert.slo.target * 100) + "% of requests served" : "-"],
      ["Error budget", alert.burn_short ? "burning " + Math.round(alert.burn_short) + " times too fast" : "-"]]));

  const cause = panel("2. Root cause", accepted ? accepted.checks.filter((c) => c.passed).length + " of " + accepted.checks.length + " checks" : diagnosis.verdict || "working", accepted ? "good" : "live",
    h("p", { class: "say" }, accepted ? sentence(accepted.summary) + "." : sentence(diagnosis.reason || "collecting evidence") + "."),
    root ? h("ol", { class: "path" }, root.path.map((name, index) => h("li", { class: index === root.path.length - 1 ? "end" : "" }, name))) : null,
    rows([["Where it starts", root ? root.service : "-"], accepted ? ["Kind of cause", accepted.cause_class.replace(/_/g, " ")] : null,
      accepted && accepted.change_id ? ["Linked change", accepted.change_id] : null]));

  const policy = data.policy;
  const decision = panel("3. Decision", policy ? (policy.verdict === "auto" ? "automatic" : policy.verdict === "approval" ? "needs approval" : "no action") : null, policy && policy.verdict === "deny" ? "bad" : "good",
    h("p", { class: "say" }, plan ? sentence(describePlan(plan)) + "." : "No automated action."),
    rows([["Allowed by", policy ? sentence(policy.reasons.join("; ")) : "-"],
      ["Rehearsal", pre ? (pre.applicable === false ? "not possible for a restart" : pre.passed ? "passed" : "failed") : "-"],
      plan && plan.inverse ? ["If it fails", "undo: " + (plan.inverse.kind === "deploy" ? "redeploy " + plan.inverse.release : "restore " + plan.inverse.value)] : null]),
    incident.state === "AWAITING_APPROVAL" ? h("p", { class: "actions" }, h("button", { class: "go", type: "button", onclick: () => decide(incident, "approved") }, "Approve"),
      h("button", { class: "no", type: "button", onclick: () => decide(incident, "rejected") }, "Reject")) : null);

  const evidencePanel = panel("4. Checks behind the diagnosis", accepted ? "accepted" : null, "good",
    accepted ? checks(accepted) : h("p", { class: "quiet" }, "No single cause has passed all its checks yet."),
    others.length ? drawer("others", others.length + " other " + (others.length === 1 ? "cause" : "causes") + " considered", ...others.map((hyp) =>
      h("div", { class: "other" }, h("b", {}, sentence(hyp.summary)), h("span", { class: "chip" + (hyp.accepted ? " live" : "") }, hyp.accepted ? "also passes" : "ruled out"), checks(hyp)))) : null);

  const live = (data.verify || {}).checks || [];
  const proof = panel("5. Proof", live.length && live.slice(-2).every((c) => c.healthy) && closed ? "proven" : pre || live.length ? "in progress" : null, "good",
    pre && pre.control ? h("div", {}, h("h4", {}, "Before acting, in a sandbox"),
      meter("Failure reproduced in the broken state", pre.control.failing_reproduced, pre.control.failing_total, "bad"),
      pre.fix ? meter("Failing requests served with the fix", pre.fix.failing.passed, pre.fix.failing.total, "good") : null,
      pre.fix ? meter("Healthy requests answered identically", pre.fix.golden.passed, pre.fix.golden.total, "good") : null) : null,
    pre && !pre.control ? h("p", { class: "quiet" }, sentence(pre.note || "")) : null,
    live.length ? h("div", {}, h("h4", {}, "After acting, on live traffic"),
      h("ul", { class: "windows" }, live.map((check) => h("li", { class: check.healthy ? "ok" : "no" },
        h("time", {}, clock(check.checked_at)), check.window.bad + " bad of " + check.window.total + ", " + check.recurrence + " repeats")))) : null,
    !pre && !live.length ? h("p", { class: "quiet" }, "Nothing has been changed, so there is nothing to prove yet.") : null);

  const EVENTS = { diagnosed: "diagnosed, plan made", approval_requested: "asked a human to approve", approved: "approved by a human", preflight_passed: "fix rehearsed in a sandbox",
    gate_passed: "still failing, plan still valid", action_done: "fix applied", proof_failed: "proof failed, undoing", closed: (closed || "closed").toLowerCase() };
  const log = panel("6. Activity log", null, "",
    h("ol", { class: "log" }, [[bundle.onset, "first customer request failed"], [incident.opened_at, "lamplighter noticed"]]
      .concat(data.timeline.map((step) => [step.ts, EVENTS[step.event] || step.event])).filter(([ts]) => ts)
      .map(([ts, text]) => h("li", {}, h("time", {}, clock(ts)), h("span", {}, text)))));

  const extra = [];
  if (data.fixforward) {
    const brief = data.fixforward, frame = brief.culprit.frame, patch = brief.patch;
    extra.push(panel("7. The line that broke", patch.status === "plausible" ? "patch suggested" : null, "live",
      h("p", { class: "say" }, frame ? brief.file + ", line " + frame.line + ", in " + frame.func + ". " + (brief.culprit.in_changed_code ? "This release added it." : "This release did not change it.") : "No stack frame was captured."),
      h("pre", {}, patch.diff || (brief.culprit.hunk ? brief.culprit.hunk.text : brief.culprit.diff)),
      h("p", { class: "quiet" }, "Model patch: " + patch.status.replace("_", " ") + ". " + sentence(patch.reason || ""))));
  }
  if (data.narrative && (data.narrative.narrative.length || data.narrative.next_steps.length)) {
    const dropped = data.narrative.dropped_ungrounded;
    extra.push(panel("8. Model notes", dropped + " removed", "",
      h("p", { class: "quiet" }, "Written by a language model. " + (dropped === 1 ? "1 unsupported sentence was" : dropped + " unsupported sentences were") + " filtered out."),
      h("ul", { class: "notes" }, data.narrative.narrative.slice(0, 6).map((s) => h("li", {}, s.text + " ", h("code", {}, s.evidence.join(" "))))),
      data.narrative.next_steps.length ? drawer("next", "Suggested next steps", h("ul", { class: "notes" }, data.narrative.next_steps.slice(0, 4).map((s) => h("li", {}, s.text + " ", h("code", {}, s.evidence.join(" ")))))) : null));
  }
  $("detail").replaceChildren(head, track(incident), tiles,
    outcome.reason ? h("p", { class: "outcome " + tone }, sentence(outcome.reason) + ".") : null,
    h("div", { class: "grid three" }, trigger, cause, decision),
    h("div", { class: "grid two" }, evidencePanel, proof),
    h("div", { class: "grid " + (extra.length ? "two" : "one") }, log, ...extra.slice(0, 1)),
    ...extra.slice(1),
    drawer("evidence", "All " + evidence.length + " pieces of evidence", h("ul", { class: "evidence" }, evidence.map((item) => h("li", {}, h("code", {}, item.id), item.summary)))),
    h("p", { class: "foot" }, incident.audit.length + " audit entries, chain head " + (incident.audit.length ? incident.audit[incident.audit.length - 1].hash.slice(0, 16) : "-")));
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
    if (selected) renderDetail(newest && newest.id === selected ? newest : await api("/v1/incidents/" + selected), status.now);
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
