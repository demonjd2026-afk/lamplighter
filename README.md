# Lamplighter

<!-- Demo thumbnail goes here once the video is recorded:
[![Watch the demo](docs/thumbnail.png)](https://www.youtube.com/watch?v=VIDEO_ID) -->

**An on-call engineer that has to prove its work.** Lamplighter watches a running system, finds the root
cause of a failure, ships the fix, and proves on recorded and live traffic that the fix worked. When it
cannot prove something, it does not act: it hands a human the evidence it has already gathered.

Built for Homingo Hack Days, Edition 01, Automation track, problem statement **B3: The AI on-call engineer**.

![Architecture](docs/architecture.png)

## Who it is for, and what it does

For teams that run services and carry a pager. When the edge starts failing its SLO, Lamplighter:

1. **Detects** it with multi-window error-budget burn (server errors and slow requests count; client errors never page).
2. **Collects evidence** by code: onset, per-service health against a baseline, call edges, error signatures, change events, and the failure path from the edge to its origin.
3. **Diagnoses** with explicit hypotheses. Each has named checks that cite evidence; one is accepted only if all its required checks pass.
4. **Rehearses the fix** in a sandbox: the failing requests must reproduce the failure in a copy of the current state, and be served in a copy with the fix, while healthy recorded traffic comes back identical.
5. **Acts** through a fixed catalogue (roll back a release, revert a config value, restart a service), under a policy enforced in code, with an idempotency key.
6. **Proves it** on live traffic, or undoes the action and escalates.
7. **Writes the postmortem**, including which line of which release broke and the diff that introduced it.

A language model is optional. It can write a narrative (filtered so that only sentences citing real
evidence with matching numbers survive) and suggest a code patch (screened, replayed in a sandbox, written
to a file, never deployed). It cannot create a fact or trigger an action. Everything above runs without it.

## Results

`python -m eval.run_eval --runs 3` starts a fresh copy of the demo shop for every run, injects one real
fault, and scores the response against ground truth. Full table and limits: [docs/EVALUATION.md](docs/EVALUATION.md).

| | Lamplighter | Naive responder (blame the latest change, else restart) |
|---|---|---|
| Correct response over 24 runs (8 fault scenarios x 3) | 24/24 | 12/24 |
| Wrong actions taken | 0 | not executed, decision only |
| Median time to detect (resolved incidents) | 2.1 s after the first failed request | |
| Median customer-visible impact (resolved incidents) | 8.9 s | |

These numbers come from the demo's timing profile, where windows are scaled down to seconds, on one small
system whose scenarios were written alongside the rules. They show the system does what it is designed to
do. They are not a claim about unseen production systems; the production defaults use 5 minute and 1 hour
windows and a 2 minute hold.

The scenario the naive responder always gets wrong is the instructive one: a dependency hangs eight seconds
after an unrelated deploy. Lamplighter rejects the deploy as a cause (the failures are not raised by that
service's code) and restarts the dependency that is unhealthy by its own telemetry.

## Install

Requires Python 3.11 or newer. No account, API key or container is needed.

```bash
git clone <your repository URL>
cd lamplighter
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
python -m demo.run_demo                          # tour: bad release, bad config, hung dependency
python -m demo.run_demo --scenario bad_release   # one scenario (see eval/scenarios.py for all eight)
python -m demo.run_demo --mode propose           # wait for a human to approve each plan in the dashboard
```

Open the dashboard at http://127.0.0.1:8088 while it runs. Each incident's postmortem path is printed.

```bash
pytest                 # 96 fast tests
pytest --e2e           # plus 3 end-to-end tests against real processes (about 3 minutes)
python -m eval.run_eval --runs 3   # the evaluation above (about 25 minutes)
```

To switch the model on (optional):

```bash
export LAMPLIGHTER_LLM_PROVIDER=anthropic        # or: openai (any OpenAI-compatible endpoint)
export LAMPLIGHTER_LLM_MODEL=<model id from your provider>
export LAMPLIGHTER_LLM_API_KEY=<your key>
python -m demo.run_demo --scenario bad_release --patch
```

To run it as a service against your own telemetry and control plane, see `.env.example` and
`lamplighter run`. That needs a `TelemetrySource` and an `Actuator` for your stack
([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#adapting-it-to-a-real-system)); the demo shop is the only
system it has been run against.

## The dashboard

A live 3D street: every service is a lamp. Amber means healthy, red means failing, unlit means not
answering. Points of light on the wires are requests, and failed ones burn out before they arrive. The
mint drone is Lamplighter: it flies to the lamp it is fixing and stays until the fix is proven. There is a
light and a dark theme (the button in the top bar, or `?theme=light`).

The dashboard source is in `ui/` and is bundled into `src/lamplighter/static/app.bundle.js`, which is
committed, so running the project needs no Node.js. To rebuild after editing: `cd ui && npm install && npm run build`.

## How it stays safe

- No policy file means propose-only. Unknown actions are denied. There is an hourly rate limit and a kill-switch file.
- A plan is bound to the state it was made against; if a human already changed things, it does not run.
- Nothing happens before a hold period, and nothing happens if the service has recovered by itself.
- Every state change and action is in a SQLite store with a hash-chained audit log (`lamplighter audit`).
- Telemetry and model output are treated as untrusted input throughout.

Details, the tests behind each control, and what is not covered: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Limits

- Five cause classes are handled. Anything else is diagnosed as far as the evidence goes and escalated.
- One fault at a time; overlapping faults escalate.
- Detection needs traffic at the edge; there are no synthetic probes yet.
- The model-patch feature runs candidate code in a local subprocess. The static screen is a filter, not a sandbox.
- A passing patch is "plausible", not "correct". It is for human review.
- Single worker process; not highly available.

## AI tools, models and libraries used

- **Building:** Claude (Anthropic) was used as a coding assistant for design, implementation and tests.
- **At run time:** no model is bundled or required. Optionally, any model reachable through the Anthropic
  Messages API or an OpenAI-compatible chat completions endpoint; the model id is supplied by the user.
- **Libraries:** FastAPI and Uvicorn (API and dashboard); pytest and httpx (tests); three.js (MIT), bundled
  into the dashboard with esbuild; the Chakra Petch typeface (SIL Open Font License, text in
  `src/lamplighter/static/fonts/OFL.txt`). Everything else, including the demo shop, is the Python standard library.
- **Design references:** OpenRCA, ITBench, a 2026 failure analysis of LLM root-cause agents, the program
  repair literature on test overfitting, and the Google SRE Workbook on SLO alerting. Links are in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#sources-for-the-design-position).

## What I would add next

- Adapters for Prometheus, Loki or OpenTelemetry, and for Kubernetes and Argo as the actuator.
- Record and replay through Keploy, so dependencies are mocked from real traffic instead of run in the sandbox.
- Synthetic probes, so a total outage with no edge traffic is still detected.
- Container isolation for model patches, and opening them as pull requests.
- More cause classes (resource exhaustion, bad feature flag, expired certificate), each with its own checks and scenarios.

## Repository map

```
src/lamplighter/   the system (see docs/ARCHITECTURE.md for a module table)
demo/shop/         the watched system: three services, a control plane, a traffic generator, three releases
demo/run_demo.py   the demo runner
eval/              fault scenarios with ground truth, the lab, the evaluation runner and its raw results
ui/                dashboard source (three.js scene and HUD); builds the committed bundle
tests/             unit, security and end-to-end tests
docs/              architecture, threat model, evaluation
```

MIT licence.
