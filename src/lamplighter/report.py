"""Postmortem rendering. Built from stored facts; model text, when present, is
confined to one labelled section and has already been through the grounding filter."""
from __future__ import annotations

import time

from .redact import redact, redact_text


def _ts(value: float | None) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(value)) + f".{int(value * 1000) % 1000:03d}Z"


def _cell(value: object) -> str:
    text = redact_text(str(value)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ").replace("\r", " ")


def render(incident: dict, actions: list[dict], audit: dict) -> str:
    data = incident["data"]
    outcome, diagnosis, bundle = data.get("outcome", {}), data.get("diagnosis", {}), data.get("bundle", {})
    plan, preflight = data.get("plan"), data.get("preflight")
    lines = [f"# Postmortem {incident['id']}", "",
             f"- **Service:** {incident['service']}",
             f"- **Outcome:** {incident['state']}: {_cell(outcome.get('reason', ''))}",
             f"- **First failure:** {_ts(bundle.get('onset'))}",
             f"- **Detected:** {_ts(incident['opened_at'])} ({outcome.get('time_to_detect_s')} s after the first failure)",
             f"- **Closed:** {_ts(incident.get('closed_at'))} ({outcome.get('time_to_close_s')} s after the first failure)",
             ""]

    lines += ["## Diagnosis", "", _cell(diagnosis.get("reason", "no diagnosis was made")), ""]
    for hyp in diagnosis.get("hypotheses", []):
        mark = "ACCEPTED" if hyp["accepted"] and diagnosis.get("accepted") == hyp["id"] else (
            "passes" if hyp["accepted"] else "rejected")
        lines += [f"### {hyp['id']} ({mark}): {_cell(hyp['summary'])}", "",
                  "| Check | Required | Result | Detail | Evidence |", "|---|---|---|---|---|"]
        for check in hyp["checks"]:
            lines.append(f"| {_cell(check['name'])} | {'yes' if check['required'] else 'no'} | "
                         f"{'pass' if check['passed'] else 'FAIL'} | {_cell(check['detail'])} | "
                         f"{', '.join(check['evidence']) or '-'} |")
        lines.append("")

    if plan:
        lines += ["## Action", ""]
        described = {"rollback_release": f"roll {plan['service']} back from {plan.get('from')} to {plan.get('to')}",
                     "revert_config": f"set {plan.get('key')} on {plan['service']} back from {plan.get('from')} "
                                      f"to {plan.get('to')}",
                     "restart": f"restart {plan['service']}"}.get(plan["kind"], plan["kind"])
        lines.append(f"- **Plan:** {_cell(described)}")
        policy = data.get("policy") or {}
        lines.append(f"- **Policy:** {policy.get('verdict', '-')} ({_cell('; '.join(policy.get('reasons', [])))})")
        for action in actions:
            lines.append(f"- **{_cell(action['kind'])}:** {action['status']} at {_ts(action['updated_at'])} "
                         f"(key `{_cell(action['key'])}`)")
        lines.append("")

    lines += ["## Proof", ""]
    if preflight:
        if not preflight.get("applicable"):
            lines.append(f"- **Before acting:** {_cell(preflight.get('note', ''))}")
        else:
            control, fix = preflight.get("control"), preflight.get("fix")
            if control:
                lines.append(f"- **Control (sandbox, current state):** {control['failing_reproduced']} of "
                             f"{control['failing_total']} failing requests reproduced the failure")
            if fix:
                lines.append(f"- **Fix (sandbox, fix applied):** {fix['failing']['passed']} of {fix['failing']['total']} "
                             f"failing requests served; {fix['golden']['passed']} of {fix['golden']['total']} "
                             f"healthy requests reproduced exactly")
            lines.append(f"- **Verdict before acting:** {'pass' if preflight.get('passed') else 'FAIL'}: "
                         f"{_cell(preflight.get('note', ''))}")
    for check in (data.get("verify") or {}).get("checks", []):
        window = check["window"]
        lines.append(f"- **Live traffic at {_ts(check['checked_at'])}:** {window['bad']} bad of {window['total']} "
                     f"requests, {check['recurrence']} recurrences of the incident's error: "
                     f"{'healthy' if check['healthy'] else 'NOT healthy'}")
    if not preflight and not (data.get("verify") or {}).get("checks"):
        lines.append("No action was taken, so there is nothing to prove.")
    lines.append("")

    brief = data.get("fixforward")
    if brief:
        culprit = brief["culprit"]
        lines += ["## Fix-forward brief", "",
                  f"The rollback restored service; release {brief['bad_release']} still needs a real fix."]
        if culprit.get("frame"):
            frame = culprit["frame"]
            inside = "inside" if culprit["in_changed_code"] else "outside"
            lines.append(f"- Failing line: `{_cell(brief['file'])}:{frame['line']}` in `{_cell(frame['func'])}`: "
                         f"`{_cell(culprit.get('line_text'))}`, {inside} the code this release changed.")
        patch = brief["patch"]
        lines.append(f"- Model patch: **{patch['status']}**: {_cell(patch.get('reason', ''))}")
        if culprit.get("hunk"):
            lines += ["", f"Changed block containing the failing line (lines {culprit['hunk']['start']} to "
                      f"{culprit['hunk']['end']}):", "", "```python", redact_text(culprit["hunk"]["text"]), "```"]
        lines += ["", "Full diff between the releases:", "", "```diff", redact_text(culprit["diff"]), "```", ""]

    narrative = data.get("narrative")
    if narrative and (narrative["narrative"] or narrative["next_steps"]):
        lines += ["## Narrative (written by a language model, filtered)", "",
                  "Only sentences that cite evidence, and whose numbers appear in that evidence, are shown. "
                  f"{narrative['dropped_ungrounded']} sentence(s) were removed by that filter.", ""]
        lines += [f"- {_cell(s['text'])} [{', '.join(s['evidence'])}]" for s in narrative["narrative"]]
        if narrative["next_steps"]:
            lines += ["", "Suggested next steps (unverified suggestions):", ""]
            lines += [f"- {_cell(s['text'])} [{', '.join(s['evidence'])}]" for s in narrative["next_steps"]]
        lines.append("")

    lines += ["## Evidence", "", "| Id | Kind | Summary | Where to check |", "|---|---|---|---|"]
    for item in redact(bundle.get("items", [])):
        lines.append(f"| {item['id']} | {item['kind']} | {_cell(item['summary'])} | `{_cell(item['source'])}` |")
    lines += ["", "## Timeline", ""]
    for step in data.get("timeline", []):
        lines.append(f"- {_ts(step['ts'])} {step['from']} -> {step['to']} ({step['event']})")
    lines += ["", "## Audit", "",
              f"Audit chain {'intact' if audit.get('intact') else 'BROKEN'} over {audit.get('entries')} entries; "
              f"last entry for this incident: `{audit.get('head')}`.", ""]
    return "\n".join(lines)
