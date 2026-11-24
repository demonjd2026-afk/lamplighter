"""The model evaluation's own arithmetic, checked with a scripted model (no network)."""
from __future__ import annotations

import json

import pytest

from eval import model_eval
from lamplighter.evidence import Bundle


def test_guard_benchmark_blocks_every_fabrication_it_is_built_for():
    guard = model_eval.guard_benchmark(model_eval.load_fixtures())
    for category, row in guard.items():
        if "known limit" in category:
            continue
        assert row["cases"] > 0 and row["correct"] == row["cases"], category
    assert guard["faithful"]["should_keep"] is True


def test_guard_benchmark_reports_its_blind_spot_instead_of_hiding_it():
    limit = model_eval.guard_benchmark(model_eval.load_fixtures())["false claim with no number (known limit)"]
    assert limit["correct"] < limit["cases"]     # if this ever fails the filter got stricter: update the docs


class Scripted:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        return self.reply


def test_narrative_run_counts_kept_and_removed_sentences():
    fixture = model_eval.load_fixtures()[0]
    bundle = Bundle.from_dict(fixture["bundle"])
    first = bundle.items[0]
    reply = json.dumps({"narrative": [{"text": first.summary, "evidence": [first.id]},
                                      {"text": "Exactly 7919 customers were affected.", "evidence": [first.id]}],
                        "next_steps": [{"text": "Review the release of checkout.", "evidence": ["E999"]}]})
    seen = []
    row = model_eval.narrative_run(Scripted(reply), fixture, judge=lambda text, context: seen.append(text) or 0.5)
    assert (row["parsed"], row["produced"], row["kept"], row["removed"]) == (True, 3, 1, 2)
    assert row["faithfulness_raw"] == 0.5 and row["faithfulness_kept"] == 0.5
    assert "7919" in seen[0] and "7919" not in seen[1]      # raw text first, filtered text second


def test_narrative_run_survives_a_reply_that_is_not_json():
    row = model_eval.narrative_run(Scripted("I cannot help with that."), model_eval.load_fixtures()[0])
    assert row["parsed"] is False and row["error"]


def test_report_renders_with_and_without_a_model():
    fixtures = model_eval.load_fixtures()
    results = {"date": "2026-01-01", "fixtures": len(fixtures), "guard": model_eval.guard_benchmark(fixtures),
               "model": None}
    assert "Not run." in model_eval.render(results)
    row = model_eval.narrative_run(Scripted('{"narrative": [], "next_steps": []}'), fixtures[0])
    results["model"] = {"name": "m", "provider": "anthropic", "runs": 1, "judge": False, "narratives": [row],
                        "patches": [{"seed": 1, "state": "RESOLVED", "status": "plausible", "reason": None}]}
    text = model_eval.render(results)
    assert "1 plausible" in text and "judge was not run" in text


def test_deepeval_judge_scores_through_the_project_client():
    pytest.importorskip("deepeval")
    # one reply that satisfies each of the metric's steps: truths, claims, verdicts
    reply = json.dumps({"truths": ["checkout failed"], "claims": ["checkout failed", "the moon is green"],
                        "verdicts": [{"verdict": "yes"}, {"verdict": "no", "reason": "not in the evidence"}]})
    judge = model_eval.deepeval_judge(Scripted(reply), "scripted")
    assert judge("checkout failed. the moon is green.", ["checkout failed"]) == 0.5
