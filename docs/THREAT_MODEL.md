# Threat model

An automated on-call engineer holds the keys to production. This document lists what could go wrong, what
stops it, the test that checks the control, and what is not covered.

## Assets

- The ability to change production (the control-plane token).
- The integrity of the audit trail.
- Whatever personal or secret data appears in logs and captured traffic.

## Trust boundaries

| Input | Trusted? | Why |
|---|---|---|
| Telemetry (logs, error messages, captured requests) | No | Written by the watched systems; error text and request bodies are attacker-influenced. |
| Change feed and control-plane responses | Partly | Authenticated source, but still validated. |
| Language-model output | No | Can be wrong, and can be steered by text in the telemetry it reads. |
| Policy file, environment | Yes | Operator-controlled. |
| API callers | Only with the token or a valid HMAC signature | |

## Threats and controls

| # | Threat | Control | Test |
|---|---|---|---|
| 1 | Prompt injection through log text makes the system take an action | The model has no tools and no action channel. Actions come from a fixed catalogue, chosen by coded checks; parameters come from change history. Model output is parsed into two text lists and nothing else. | `test_narrative_survives_prompt_injection_in_logs` |
| 2 | The model invents facts in the postmortem | Grounding filter: a sentence is kept only if every citation exists and every number it states appears in the cited evidence. The section is labelled as model-written. | `test_only_cited_and_numerically_supported_sentences_survive` |
| 3 | A model-written patch is malicious or wrong | Off by default. Static screen (no new imports, no `eval`/`exec`/`open`/dunder access, entry points kept), then replay in a sandbox. A passing patch is labelled "plausible" and written to a file; it is never deployed. | `test_static_screen_rejects_dangerous_patches`, `test_wrong_and_hostile_patches_are_rejected_by_replay_and_screen` |
| 4 | Secrets or personal data leak to the model provider or into reports | Redaction of tokens, keys, emails, phone and card-like numbers and sensitive keys before anything is sent or rendered. Only evidence summaries are sent, not raw logs. | `test_secrets_in_evidence_are_redacted_before_they_reach_the_model`, `test_incident_view_redacts_secrets` |
| 5 | A wrong diagnosis changes production | Required checks; sandbox control and fix replays before acting; hold period; staleness precondition; postflight proof with automatic undo. | `tests/test_orchestrator.py`, `eval/` scenarios |
| 6 | The automation acts repeatedly or runs away | Hourly rate limit, one open incident per service, latch and cooldown, kill-switch file, `observe` and `propose` modes. Fail-closed defaults: no policy file means propose-only. | `test_policy_tiers_allow_lists_rate_limit_and_kill_switch`, `test_kill_switch_stops_a_pre_approved_action` |
| 7 | Duplicate or replayed actions after a crash or a lost response | Intent recorded before the call; idempotency key on every mutation. | `test_lost_response_does_not_cause_a_second_action` |
| 8 | Unauthenticated approval of a plan | Bearer token compared in constant time; decisions bind to the plan hash and to the AWAITING_APPROVAL state; the first decision wins. Routes with no configured secret are disabled. | `test_decision_requires_token_matching_plan_and_waiting_incident`, `test_with_no_token_configured_everything_protected_is_closed` |
| 9 | Forged or replayed alert webhooks | HMAC-SHA256 over timestamp and body, 5 minute tolerance, 16 KiB cap, service must exist in the catalogue, effect is deduplicated. | `test_webhook_checks_signature_freshness_and_service` |
| 10 | Server-side request forgery (replay or actions pointed at an internal address) | Every outbound URL passes an allow-list of hosts; plain HTTP only on loopback; no credentials in URLs. Replay targets come from the control plane and are checked again. | `test_urls_outside_the_allow_list_are_refused`, `test_replay_refuses_targets_outside_the_allow_list` |
| 11 | Header or request injection through captured traffic | Only GET/POST/PUT/PATCH/DELETE with a plain absolute path are replayed. | `test_only_ordinary_captured_requests_are_replayed` |
| 12 | Path traversal (incident ids, release names, static files) | Strict patterns, resolved-path containment, a two-file static allow-list. | `test_incident_ids_are_validated`, `test_release_reader_cannot_leave_the_releases_root`, `test_security_headers_and_static_allow_list` |
| 13 | Cross-site scripting in the dashboard or postmortem | The dashboard inserts server data with `textContent` only; CSP `default-src 'none'; script-src 'self'`; no inline script; markup in reports is escaped. | `test_security_headers_and_static_allow_list`, `test_report_neutralises_markup_from_telemetry` |
| 14 | Malformed telemetry crashes or misleads the pipeline | Type validation on every record; bad lines are skipped and counted (`lamplighter_telemetry_skipped_lines`). | `test_malformed_records_are_skipped_and_counted` |
| 15 | Tampering with the audit trail | SHA-256 hash chain; `lamplighter audit` verifies it. | `test_audit_chain_detects_tampering`, `test_audit_chain_detects_deletion` |
| 16 | Replaying non-idempotent traffic damages production | Replays run only against sandbox stacks, never the live system. | by construction (`prove.py`) |

## Not covered

- **Transport security and rate limiting.** The API binds to loopback and speaks plain HTTP. Put it behind a
  TLS-terminating reverse proxy with request limits before exposing it.
- **A real sandbox for model patches.** The static screen is a filter, not isolation. The demo runs a
  candidate patch as a local subprocess. Run it in a container with no network and no credentials before
  using that feature outside a demo.
- **Semantic truth of the narrative.** The grounding filter checks citations and numbers. A sentence that
  cites real evidence and misstates what it means can pass. That is why the section is labelled and why
  nothing in it is executed.
- **Compromise of the host or the database file.** The audit chain detects edits; it does not prevent them,
  and an attacker who can rewrite the whole chain can forge it. Ship the head hash elsewhere if that matters.
- **Redaction completeness.** Pattern-based redaction misses secrets in unusual formats.
- **Poisoned telemetry aimed at the checks themselves.** An attacker who can forge both a change event and
  matching error logs could steer a diagnosis toward a rollback. The sandbox control replay limits this
  (the failure must really reproduce), and the action set is limited to reversible operations.
- **Single-tenant, single-token access control.** There are no roles or per-user identities.
- **Dependencies.** Runtime dependencies are FastAPI and Uvicorn; versions are bounded, not pinned by hash.
