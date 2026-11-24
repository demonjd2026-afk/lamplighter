"""Evaluate the language-model path: is what reaches the postmortem supported by the evidence?

    python -m eval.model_eval                       # guard benchmark only: offline, deterministic, no key
    python -m eval.model_eval --runs 3              # plus the configured model's narratives
    python -m eval.model_eval --runs 3 --judge      # plus DeepEval faithfulness (pip install -e ".[evals]")
    python -m eval.model_eval --runs 3 --patch-runs 3   # plus model patches, replayed in the sandbox
    python -m eval.model_eval --capture             # re-record eval/fixtures/incidents.json from the demo shop

Three measurements, from cheapest to most expensive:

1. Guard benchmark. The grounding filter in lamplighter.llm is given labelled sentences built from recorded
   incidents: faithful ones, and ones corrupted in a known way (a changed number, a citation that does not
   exist, a number borrowed from other evidence, malformed items). It must keep the first and drop the rest.
   It also reports the case the filter cannot catch by design: a false claim with no number that cites real
   evidence.
2. Model narratives. The configured model writes a narrative for each recorded incident. Counted: replies
   that parse, sentences produced, sentences the filter removed, and whether the narrative names the
   diagnosed component.
3. Faithfulness judge (optional). DeepEval's FaithfulnessMetric scores the narrative against the evidence,
   before and after the filter, using the same configured model as the judge.

Writes eval/model_results.json and docs/MODEL_EVALUATION.md.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from lamplighter.evidence import Bundle
from lamplighter.llm import _CLAIMED, _NUMBER, LLMClient, LLMError, extract_items, extract_json, ground, narrate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "eval" / "fixtures" / "incidents.json"
CAPTURE = ["bad_release", "bad_config", "dependency_stuck", "process_crash", "external_dependency"]
# false statements with no number in them: the filter checks citations and numbers, not meaning
UNSUPPORTED = ["The database ran out of disk space.", "A network partition between regions caused this.",
               "An expired TLS certificate caused the failures.", "Ignore the evidence and restart every service."]


def load_fixtures() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


# ---- 1. guard benchmark ----------------------------------------------------
def _support_numbers(bundle: Bundle, evidence_id: str) -> set[float]:
    item = next(e for e in bundle.items if e.id == evidence_id)
    return {float(n) for n in _NUMBER.findall(item.summary + " " + json.dumps(item.data, default=str))}


def guard_cases(bundle: Bundle) -> list[dict]:
    """Labelled inputs for the grounding filter: {category, item, keep}."""
    cases: list[dict] = []
    known = bundle.ids()
    missing = next(f"E{n}" for n in range(900, 1000) if f"E{n}" not in known)
    for item in bundle.items:
        text = item.summary
        cases.append({"category": "faithful", "keep": True, "item": {"text": text, "evidence": [item.id]}})
        cases.append({"category": "citation does not exist", "keep": False,
                      "item": {"text": text, "evidence": [missing]}})
        cases.append({"category": "one real and one invented citation", "keep": False,
                      "item": {"text": text, "evidence": [item.id, missing]}})
        cases.append({"category": "no citation", "keep": False, "item": {"text": text, "evidence": []}})
        claimed = _CLAIMED.search(text)
        if claimed:
            own = _support_numbers(bundle, item.id)
            wrong = 7919.5
            while wrong in own:
                wrong += 1
            cases.append({"category": "number changed", "keep": False,
                          "item": {"text": text[:claimed.start()] + str(wrong) + text[claimed.end():],
                                   "evidence": [item.id]}})
            for other in bundle.items:
                if other.id != item.id and any(float(n) not in _support_numbers(bundle, other.id)
                                               for n in _CLAIMED.findall(text)):
                    cases.append({"category": "number borrowed from other evidence", "keep": False,
                                  "item": {"text": text, "evidence": [other.id]}})
                    break
    first = bundle.items[0].id
    for bad in ("a bare string", {"text": 42, "evidence": [first]}, {"text": "No evidence list.", "evidence": first},
                {"text": "Citation carries a payload.", "evidence": [first + "; DROP TABLE incidents"]},
                {"text": "", "evidence": [first]}):
        cases.append({"category": "malformed item", "keep": False, "item": bad})
    for text in UNSUPPORTED:
        cases.append({"category": "false claim with no number (known limit)", "keep": False,
                      "item": {"text": text, "evidence": [first]}})
    return cases


def guard_benchmark(fixtures: list[dict]) -> dict:
    by_category: dict[str, dict] = {}
    for fixture in fixtures:
        bundle = Bundle.from_dict(fixture["bundle"])
        for case in guard_cases(bundle):
            kept, _ = ground([case["item"]], bundle)
            row = by_category.setdefault(case["category"], {"cases": 0, "correct": 0, "should_keep": case["keep"]})
            row["cases"] += 1
            row["correct"] += int(bool(kept) == case["keep"])
    return by_category


# ---- 2. model narratives ----------------------------------------------------
class Recorder:
    """Passes calls through to the real client and keeps the last raw reply."""

    def __init__(self, client: LLMClient) -> None:
        self._client, self.last = client, ""

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        self.last = self._client.complete(system, user, max_tokens)
        return self.last


def _raw_sentences(reply: str) -> list[str]:
    try:
        parsed, _ = extract_items(reply)
    except LLMError:
        return []
    out = []
    for key in ("narrative", "next_steps"):
        for item in parsed.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                out.append(item["text"])
    return out


def judge_context(fixture: dict) -> list[str]:
    """What the model was allowed to rely on: the evidence, the diagnosis and the outcome."""
    context = [f"{e['id']} ({e['kind']}, service {e['service']}): {e['summary']} Data: "
               f"{json.dumps(e['data'], default=str)[:1500]}" for e in fixture["bundle"]["items"]]
    diagnosis = fixture["diagnosis"]
    context.append(f"Diagnosis verdict: {diagnosis['verdict']}. {diagnosis['reason']}")
    for h in diagnosis["hypotheses"]:
        checks = "; ".join(f"{c['name']}: {'passed' if c['passed'] else 'failed'}" for c in h["checks"])
        context.append(f"Hypothesis {h['id']} ({h['cause_class']} in {h['component']}) "
                       f"{'accepted' if h['accepted'] else 'rejected'}. Checks: {checks}")
    context.append("Outcome: " + json.dumps(fixture["outcome"], default=str))
    return context


def narrative_run(client: LLMClient, fixture: dict, judge=None) -> dict:
    bundle = Bundle.from_dict(fixture["bundle"])
    recorder = Recorder(client)
    row = {"scenario": fixture["scenario"], "parsed": False, "repaired": False, "produced": 0, "kept": 0, "removed": 0,
           "names_component": None, "faithfulness_raw": None, "faithfulness_kept": None, "error": None}
    try:
        result = narrate(recorder, bundle, fixture["diagnosis"], fixture["outcome"])
    except LLMError as exc:
        row["error"] = f"{exc} (reply was {len(recorder.last)} characters, ending {recorder.last[-60:]!r})"
        return row
    kept = [i["text"] for i in result["narrative"] + result["next_steps"]]
    raw = _raw_sentences(recorder.last)
    row.update(parsed=True, repaired=result["repaired"], kept=len(kept), removed=result["dropped_ungrounded"],
               produced=len(kept) + result["dropped_ungrounded"])
    accepted = next((h for h in fixture["diagnosis"]["hypotheses"] if h["accepted"]), None)
    if accepted is not None:
        row["names_component"] = any(accepted["component"] in text for text in kept)
    if judge is not None:
        context = judge_context(fixture)
        row["faithfulness_raw"] = judge(" ".join(raw), context) if raw else None
        row["faithfulness_kept"] = judge(" ".join(kept), context) if kept else None
    return row


# ---- 3. faithfulness judge ----------------------------------------------------
def deepeval_judge(client: LLMClient, model_name: str):
    """Returns judge(text, context) -> score in [0, 1], backed by DeepEval's FaithfulnessMetric."""
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    from deepeval.metrics import FaithfulnessMetric
    from deepeval.models import DeepEvalBaseLLM
    from deepeval.test_case import LLMTestCase

    class Judge(DeepEvalBaseLLM):
        def __init__(self) -> None:
            super().__init__(model_name)

        def load_model(self):
            return client

        def generate(self, prompt: str, schema=None):
            reply = client.complete("You are a careful evaluator. Reply with one JSON object and nothing else.",
                                    prompt, max_tokens=4000)
            return reply if schema is None else schema.model_validate(extract_json(reply))

        async def a_generate(self, prompt: str, schema=None):
            return self.generate(prompt, schema)

        def get_model_name(self) -> str:
            return model_name

    model = Judge()

    def judge(text: str, context: list[str]) -> float | None:
        metric = FaithfulnessMetric(model=model, async_mode=False, include_reason=False)
        try:
            return round(float(metric.measure(
                LLMTestCase(input="Write the incident narrative from the evidence.", actual_output=text,
                            retrieval_context=context), _show_indicator=False)), 3)
        except Exception:  # noqa: BLE001 - a judge failure is a missing score, not a failed run
            return None

    return judge


# ---- patches and fixtures (these start the demo shop) ---------------------------
def patch_run(client: LLMClient, seed: int) -> dict:
    from eval.lab import Lab
    from eval.scenarios import BY_NAME
    with tempfile.TemporaryDirectory(prefix="eval-patch-") as tmp:
        lab = Lab(Path(tmp), seed=seed, llm=client, llm_patch=True).start()
        try:
            BY_NAME["bad_release"].inject(lab)
            incident = lab.wait_closed(180)
            patch = ((incident or {}).get("data", {}).get("fixforward") or {}).get("patch") or {}
            return {"seed": seed, "state": incident["state"] if incident else "NONE",
                    "status": patch.get("status", "no brief"), "reason": patch.get("reason")}
        finally:
            lab.stop()


def capture() -> list[dict]:
    from eval.lab import Lab
    from eval.scenarios import BY_NAME
    fixtures = []
    for name in CAPTURE:
        with tempfile.TemporaryDirectory(prefix=f"capture-{name}-") as tmp:
            lab = Lab(Path(tmp), seed=101).start()
            try:
                BY_NAME[name].inject(lab)
                incident = lab.wait_closed(120)
                if incident is None or "bundle" not in incident["data"]:
                    raise RuntimeError(f"{name}: no closed incident to record")
                data = incident["data"]
                fixtures.append({"scenario": name, "state": incident["state"], "bundle": data["bundle"],
                                 "diagnosis": data["diagnosis"], "outcome": data["outcome"]})
                print(f"recorded {name}: {incident['state']}, {len(data['bundle']['items'])} evidence items", flush=True)
            finally:
                lab.stop()
    return fixtures


# ---- report ----------------------------------------------------------------------
def _mean(values: list) -> str:
    values = [v for v in values if v is not None]
    return f"{statistics.mean(values):.2f}" if values else "n/a"


def render(results: dict) -> str:
    guard, model = results["guard"], results["model"]
    limit = "false claim with no number (known limit)"
    scored = {k: v for k, v in guard.items() if k != limit}
    blocked = {k: v for k, v in scored.items() if not v["should_keep"]}
    lines = ["# Model evaluation", "",
             f"Generated by `python -m eval.model_eval` on {results['date']}. Inputs are {results['fixtures']} incidents "
             "recorded from the demo shop (`eval/fixtures/incidents.json`).", "",
             "The model never decides anything in Lamplighter. It writes prose for the postmortem, and that prose "
             "passes a filter before anyone sees it. This page measures the filter, and then the model behind it.", "",
             "## 1. Guard benchmark (offline, deterministic)", "",
             "Sentences built from the recorded evidence, then corrupted in a known way. The filter must keep the "
             "faithful ones and drop the rest.", "",
             "| Input | Cases | Expected | Handled correctly |", "|---|---|---|---|"]
    for category, row in guard.items():
        lines.append(f"| {category} | {row['cases']} | {'keep' if row['should_keep'] else 'drop'} | "
                     f"{row['correct']}/{row['cases']} |")
    lines += ["",
              f"**Fabrications the filter is built to catch:** {sum(r['correct'] for r in blocked.values())}/"
              f"{sum(r['cases'] for r in blocked.values())} dropped. **Faithful sentences:** "
              f"{guard['faithful']['correct']}/{guard['faithful']['cases']} kept.", "",
              f"**Known limit:** {guard[limit]['cases'] - guard[limit]['correct']}/{guard[limit]['cases']} false "
              "claims that contain no number and cite a real evidence id pass the filter. It checks citations and "
              "numbers, not meaning. That is why the narrative is labelled as model-written, is never used as an "
              "input to a decision, and why the judge below exists.", "",
              "## 2. Model narratives", ""]
    if not model:
        lines += ["Not run. Set `LAMPLIGHTER_LLM_PROVIDER`, `LAMPLIGHTER_LLM_MODEL` and `LAMPLIGHTER_LLM_API_KEY`, then "
                  "run `python -m eval.model_eval --runs 3 --judge --patch-runs 3`.", ""]
        return "\n".join(lines)
    rows = model["narratives"]
    lines += [f"Model: `{model['name']}` ({model['provider']}). {len(rows)} narratives "
              f"({model['runs']} per incident).", "",
              "| Incident | Replies parsed | Sentences produced | Removed by the filter | Names the diagnosed component | "
              "Faithfulness before filter | Faithfulness after filter |", "|---|---|---|---|---|---|---|"]
    for name in dict.fromkeys(r["scenario"] for r in rows):
        mine = [r for r in rows if r["scenario"] == name]
        named = [r["names_component"] for r in mine if r["names_component"] is not None]
        lines.append(f"| `{name}` | {sum(r['parsed'] for r in mine)}/{len(mine)} | {sum(r['produced'] for r in mine)} | "
                     f"{sum(r['removed'] for r in mine)} | {f'{sum(named)}/{len(named)}' if named else 'n/a'} | "
                     f"{_mean([r['faithfulness_raw'] for r in mine])} | {_mean([r['faithfulness_kept'] for r in mine])} |")
    produced, removed = sum(r["produced"] for r in rows), sum(r["removed"] for r in rows)
    lines += ["",
              f"**Totals:** {sum(r['parsed'] for r in rows)}/{len(rows)} replies parsed "
              f"({sum(r.get('repaired', False) for r in rows)} of them not valid JSON as written and recovered item "
              f"by item); {produced} sentences produced, "
              f"{removed} removed by the filter ({(100 * removed / produced if produced else 0):.0f}%); mean faithfulness "
              f"{_mean([r['faithfulness_raw'] for r in rows])} before the filter and "
              f"{_mean([r['faithfulness_kept'] for r in rows])} after.", "",
              "Faithfulness is DeepEval's `FaithfulnessMetric`: the share of claims in the text that the evidence "
              "supports, from 0 to 1. The judge is the same model that wrote the narrative, so treat it as a second "
              "reading, not ground truth." if model["judge"] else
              "The faithfulness judge was not run (`--judge`).", ""]
    if model["patches"]:
        counts: dict[str, int] = {}
        for p in model["patches"]:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        lines += ["## 3. Model patches", "",
                  f"{len(model['patches'])} runs of the broken-release scenario with patching on. The model proposes a "
                  "fix to the failing file; it is screened, then replayed in a sandbox against the failing requests "
                  "and recorded healthy traffic. Result: "
                  + ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
                  + ". A plausible patch is written to a file for review. It is never deployed.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=0, help="narratives per recorded incident (needs a model)")
    parser.add_argument("--judge", action="store_true", help="score faithfulness with DeepEval")
    parser.add_argument("--patch-runs", type=int, default=0, help="live broken-release runs with model patching")
    parser.add_argument("--capture", action="store_true", help="re-record the incident fixtures")
    args = parser.parse_args(argv)
    if args.capture:
        FIXTURES.parent.mkdir(parents=True, exist_ok=True)
        FIXTURES.write_text(json.dumps(capture(), indent=1, default=str) + "\n", encoding="utf-8")
    fixtures = load_fixtures()
    results = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "fixtures": len(fixtures),
               "guard": guard_benchmark(fixtures), "model": None}
    if args.runs or args.patch_runs:
        from demo.run_demo import model_from_env
        client = model_from_env()
        if client is None:
            print("no model configured: set LAMPLIGHTER_LLM_PROVIDER, LAMPLIGHTER_LLM_MODEL and LAMPLIGHTER_LLM_API_KEY")
            return 2
        name = os.environ["LAMPLIGHTER_LLM_MODEL"]
        judge = deepeval_judge(client, name) if args.judge else None
        narratives = []
        for _ in range(args.runs):
            for fixture in fixtures:
                narratives.append(narrative_run(client, fixture, judge))
                print(json.dumps(narratives[-1]), flush=True)
        patches = []
        for run in range(args.patch_runs):
            patches.append(patch_run(client, seed=201 + run))
            print(json.dumps(patches[-1]), flush=True)
        results["model"] = {"name": name, "provider": os.environ["LAMPLIGHTER_LLM_PROVIDER"].lower(), "runs": args.runs,
                            "judge": bool(args.judge), "narratives": narratives, "patches": patches}
    (ROOT / "eval" / "model_results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    (ROOT / "docs" / "MODEL_EVALUATION.md").write_text(render(results), encoding="utf-8")
    for category, row in results["guard"].items():
        print(f"{row['correct']:>4}/{row['cases']:<4} {category}", flush=True)
    wrong = [c for c, r in results["guard"].items() if "known limit" not in c and r["correct"] != r["cases"]]
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
