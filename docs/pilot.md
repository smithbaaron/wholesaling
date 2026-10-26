# Synthetic investor pilot (build order BO-09)

REQUIREMENTS.md BO-09 ("10 to 20 synthetic investor profiles and controlled test mailboxes
before real outreach"), BO-20 ("all agents pass end-to-end tests with synthetic data"), INV-22,
INV-23, INV-24, POL-04. Code: `wholesaling/pilot/`, `wholesaling/api/routes/pilot.py`,
`tests/pilot/`, `tests/e2e/test_pilot.py`.

## What it is

Sixteen named scenarios drive fifteen fake investors (fourteen with an email address, one
without) through the entire investor loop **in mock mode**, on a fresh SQLite database, with no
Temporal server, no mailbox and no model:

```
M1 (21 seed markets)  ->  A1 + A2 on Houston, TX and Columbus, OH  ->  the WF-02 starter
(activities/sequence_starter.py) lists each market's eligible investors, priority tier first, and
starts one InvestorSequenceWorkflow per persona with a primary email (no email / suppressed:
skipped)  ->  every sequence draft is A5-reviewed before the policy decision  ->  scripted replies
injected into the mock Graph gateway and announced twice through POST /webhooks/graph  ->
GraphInboundMessageWorkflow (A4 classify -> buy-box versioned upsert -> A4 draft -> A5 QA ->
policy engine)  ->  the operator approves every queued draft through POST /review/{id}/approve
one simulated hour later  ->  sends through the mock gateway in the original conversation  ->
delta-poll replay  ->  report
```

Every piece in that chain is the production code: the activities in `activities/{discovery,
sequence_starter,outreach,conversation,graph}.py`, the repositories, `policy/engine.py`, the
workflow `run()` loops in `workflows/investor_sequence.py` and `workflows/graph_maintenance.py`,
the webhook route, the review route (same auth, same `human_decision` signal, same audit events)
and the policies route. The pilot adds one stand-in:

1. `pilot/sim.py` drives the workflow loops. It swaps the two workflow modules' `workflow` name
   for a shim (the technique of `tests/workflows/test_investor_sequence_sim.py`) so
   `execute_activity` runs the real activity, `wait_condition` parks the workflow until a signal
   or a simulated deadline, and `now()` reads a simulated clock that the Graph, conversation and
   starter activity contexts and the mock Graph gateway share. Exactly one workflow runs at a time
   and ties resolve in registration order, so a run is deterministic. Its `SimTemporalGateway`
   starts workflows by name and delivers signals the way the Temporal SDK does
   (`handler(*args)`, dataclass payloads mapped by field name onto the handler's `X | None`
   annotation). The starter's candidates are scheduled two simulated minutes apart (one workflow
   at a time) instead of letting `run_outreach_kickoff` start them in one pass, and `run_a2` is
   called with `start_sequence=False` so starts happen on the simulated clock.

The two earlier stand-ins (an A5 approval written for the L1 persona's sequence draft, GAP-04; an
`inbound.workflow_started` audit event written for queued replies, GAP-05) are gone: the A3 path
now calls A5 itself and the workflow passes its own id into `fetch_message` /
`draft_and_qa_reply`.

Model outputs are scripted per inbound message on `MockModelProvider` (`ScriptBook` in
`pilot/runner.py`): realistic A4 classifications, extractions, drafts and A5 verdicts keyed on a
phrase of the investor's message, so the deterministic processing around the model (quote
stripping, auto-reply detection, unknown tracking, corrections, multi-box keys, sensitive-content
scan, QA pre-checks, policy conditions) runs on real-looking data. Any model call no script covers
is listed in the report as "unscripted".

## The scenarios

| id | persona (market, A1 score) | path | proves |
|---|---|---|---|
| `priority_no_reply` | Bayou Capital Partners LLC (Houston, 88) | never replies | 4 touches, one conversation, sequence completed; priority tier (INV-10, INV-23, INV-04) |
| `l1_auto_send_then_price_talk` | Lone Star Flips LLC (Houston, 75) | interested reply, then a pricing/contract question | with `investor_initial_outreach` and `investor_reply` scoped to L1: A5-approved initial auto-sends through the real path, clean reply auto-sends, price/contract talk queues (POL-03a, POL-03c, POL-09, XC-01) |
| `partial_then_correction` | Heights Holdings LLC (Houston, 71) | partial buy box, then "I said 150k max, not 250k" | version 1 with unknowns, version 2 with `correction: price_max` history (INV-14, INV-21) |
| `two_buy_boxes` | Keller Realty Investments LLC (Houston, 64, agent-investor) | two criteria sets in one email | two keyed version-1 rows (BB-03) |
| `not_interested` | Fast Close Houston LLC (Houston, 61, wholesaler) | decline after touch 2 | no draft, cancel `reply` after 2 touches |
| `prospect_interested_full_buy_box` | Maria Hernandez (Houston, 58, individual) | complete buy box after touch 1 | version 1 with no unknowns; prospect tier |
| `no_contact_found` | Gulf Coast Rentals LLC (Houston, 76) | A2 finds no email | the starter skips it (`no_email`); `run_a2` wrote `investor.unreachable` (INV-08) |
| `bounce_after_touch_1` | Harbor Capital LP 6 (Columbus, 84) | NDR from postmaster, new conversation | `bounce` suppression, `bounce` signal, cancel after 1 touch (INT-25) |
| `opt_out_after_touch_2` | Summit Equity LLC 5 (Columbus, 77) | "Unsubscribe" | `opt_out` suppression at every level, cancel after 2 touches (POL-03l, INV-15) |
| `question` | Prairie Property LLC 9 (Columbus, 68) | asks about sourcing | reply drafted, no buy box invented |
| `not_now_with_date` | Summit Holdings Inc 7 (Columbus, 67) | "circle back January 15, 2027" | `reconnect_date` captured (INV-15) |
| `ooo_then_real_reply` | Harbor Assets Inc 2 (Columbus, 66) | automatic reply, then a real one after touch 2 | auto-reply never cancels; the real reply does (INV-22, INV-10) |
| `sarcasm` | Blue Capital LLC 0 (Columbus, 65) | "Oh sure, I LOVE spam" | `not_interested`, no draft (INV-22) |
| `quoted_old_text` | Pine Property Trust 3 (Columbus, 62) | reply quoting our email | `quoted_text_stripped`, buy box from the new text only (INV-22) |
| `suppressed_before_start` | River Holdings LLC 4 (Columbus, 56) | address suppressed by hand first | the starter never starts a sequence (`suppressed`), nothing drafted (POL-03l, WF-02) |
| `priority_before_prospect` | cohort | - | within each market, priority-tier sequences start before prospects (INV-04) |

Each persona scenario declares its expected end state (`Scenario.expected(level,
approve_everything)`): sequence status and cancel reason, touches sent and how many went out
without a human, inbound rows, classification sequence, reply drafts / sent / auto-sent, items
that waited in the review queue, buy-box versions per key, corrections, suppression reason, model
calls, single conversation, tier, the starter's skip reason and whether `investor.unreachable`
was recorded; plus per-message checks (`is_auto_reply`, `quoted_text_stripped`,
`reconnect_date`, the signal sent to the sequence, the QA verdict). The same catalogue grades an
L0, L1, L2 or no-approval run: at L1 every sequence draft is A5-approved (locked templates), so
initials auto-send (POL-03a) and, the initial having gone out, follow-ups do too (POL-03b).

## Running it

```bash
uv run wholesaling-pilot run                       # L0, Houston + Columbus, SQLite in memory
uv run wholesaling-pilot run --level L1 --out report.md --json pilot-report.json
uv run wholesaling-pilot run --no-approve          # operator approves nothing
uv run wholesaling-pilot run --market Houston      # Columbus personas are reported as skipped
uv run wholesaling-pilot run --db sqlite+aiosqlite:///pilot.db   # or TEST_DATABASE_URL (Postgres)
uv run wholesaling-pilot run --temporal            # see "Temporal mode" below
```

Exit status 0 when every scenario passes (a scenario that fails only on the checks a documented
known defect covers counts as passing; `--strict` makes it fail; no scenario carries a known
defect today), 1 when a scenario fails or an invariant breaks, 2 when `--temporal` cannot start
the test server. A run takes about four seconds of wall time for fourteen simulated days.

`GET /pilot/report?format=json|text|markdown` (operator Basic auth, like `/review`) serves the
newest report: the one stored as a `pilot.completed` audit event in the API's database when the
pilot ran against it, otherwise the JSON file at `PILOT_REPORT_PATH` (default
`pilot-report.json`).

Tests: `tests/pilot/` (catalogue consistency against the agents' schemas and the A5 denylist,
derived expectations, the simulator, the gateway's signal semantics, the CLI, the route) and
`tests/e2e/test_pilot.py` (the full pilot at L0, L1 and with no approvals; this is the BO-20
"all agents pass end-to-end tests with synthetic data" for the investor side).

## Reading the report

- **Verdict**: `PASS`, `PASS (with known defects)` or `FAIL`. A known defect is a scenario whose
  only failing checks are the ones its `known_defects` entry names (none today; GAP-02's
  `ooo_then_real_reply` was the last and is fixed). `tests/e2e/test_pilot.py` keeps
  `KNOWN_DEFECT_SCENARIOS` in step with the catalogue.
- **Scenarios**: one line per scenario with its failed checks (`expected` vs `actual`).
- **Invariants** (whole run): INV-23 one conversation per sequence; INV-24 one inbound row and
  one `message.received` event per injected message although each notification was POSTed twice
  and the delta poll replayed every message; the webhook reported each duplicate; RUL-05 one mock
  send per sent row; POL-03l no send to a suppressed address after suppression; POL-03c replies
  auto-sent only where the level allows; XC-02 one `agent_runs` row per model call; no workflow
  crashed or was left waiting without a timeout.
- **Aggregates**: discovery counts, sequences by outcome, touches by step, inbound /
  classification histogram, buy boxes (current / versions / corrections), suppressions, drafts /
  sent / auto-sent / blocked, queue items and approvals, QA verdicts and flags, policy decisions by
  `action:decision`, model calls by `agent:task`, tokens and (mock-priced) cost, workflow counts,
  signal contract errors.
- **POL-04 promotion readiness**: the numbers `/policies` computes (`reporting/kpis.py`) over the
  pilot's audit trail - human-approved drafts, edited share, QA-blocked sent, compliance flags -
  per investor action. A full L0 run yields 12 / 6 / 9 approvals for initial / follow-up / reply,
  so nothing is ready (the bar is 50); the point is that the numbers come out of the same path a
  live L0 month would feed.
- **Signal contract errors**: every signal whose payload the handler could not accept (the
  GAP-01 regression guard; zero since the fix).
- **Gaps**: open gaps evidenced by the run (today only the by-design GAP-06 note; GAP-01 reappears
  only if a payload is rejected again).

Baseline L0 run after the BO-09 follow-up (seed 20261027): 21 markets scored (0 Active, 20
Watch), 21 investor rows in the two markets, 15 qualified, 5 priority, 14 with an email; the
starter skipped 1 `no_email` (Houston) and 1 `suppressed` (Columbus); 13 sequences (1 completed;
cancelled reply 10, bounce 1, opt_out 1); 19 touches (13 / 4 / 1 / 1 by step, 1 auto); 15
inbound (1 bounce, 1 auto-reply; buy_box_supplied 7, question 2, not_interested 2, not_now 1,
opt_out 1, ambiguous 1); 7 current buy boxes over 8 versions with 1 correction; suppressions
bounce 1, opt_out 1, manual 1; 10 reply drafts, 10 sent (1 auto), 0 blocked; 27 queue items, 27
approvals, 0 edits; QA approve 29 (19 sequence drafts + 10 replies), flag `sensitive_content` 1;
59 model calls = 59 `agent_runs` (A4 classify 13, extract 7, draft 10; A5 qa 29), $0.1148 at mock
pricing; 28 workflows, 0 failed; 0 signal contract errors. At L1 the same run auto-sends all 19
touches and 9 of 10 replies (the price-talk reply queues) with 1 approval.

Before the follow-up the same seed gave `PASS (with known defects)` at L0 and L1: 15 pass + 1
known defect (`ooo_then_real_reply`, GAP-02), 14 sequences, 18 touches, 14 inbound, 36 model
calls and 11 signal contract errors per run.

## What it does not prove

- **No real mailbox.** The mock Graph gateway threads by `conversationId` and refuses to send an
  unknown or already-sent draft, but Outlook's real threading, NDR formats, header casing and
  Graph's webhook retry behaviour are untested (PRE-05 is open). The bounce persona uses the
  `detect_bounce` heuristics on a synthetic NDR body.
- **No real model.** A4 and A5 outputs are scripted. The pilot proves the pipeline contract and
  every deterministic step around the model; it says nothing about classification accuracy,
  extraction quality or draft quality. That is BO-13's eval harness over
  `tests/agents/golden/inbound_replies.json`.
- **No real Temporal in the default mode.** The simulator executes the workflow code's control
  flow with the real activities, but durability, retries, replay, timers across worker restarts and
  the `human_decision` wait's 14-day timeout under a real server are only covered by the existing
  `temporal`-marked tests and by `--temporal` (below).
- **No production fixtures.** The personas are the mock provider's committed fixtures; A1/A2 on a
  real data provider (INT-02/INT-03) is a separate verification.
- **No seller side.** BO-14 is the seller pilot.
- **A5's judgement of the locked templates is scripted** (`A3_TEMPLATE_QA` in
  `pilot/scenarios.py`: approve at 0.95-0.97). The pilot proves the A3 -> A5 -> policy -> send
  path produces and consumes the QA verdict at L1; whether the real model approves CPY-01 is a
  BO-13 eval question. The deterministic pre-checks (XC-07 denylist, suppression, recipient,
  cadence) do run for real on every sequence draft.

## Temporal mode (`--temporal`)

`pilot/temporal_driver.py` runs the same scenarios through `temporalio.testing.
WorkflowEnvironment.start_time_skipping()` with the real worker (`workers.main.build_worker`) and
`LiveTemporalGateway` over the environment's client, so the webhook, the review route and the
conversation activities start and signal workflows through Temporal. Time moves with `env.sleep`;
the driver polls the database for newly sent touches, injects replies when due, approves queued
drafts through the route and stops when every sequence is closed or nothing has changed for three
simulated days. Sequences start through the production starter
(`sequence_starter.start_investor_sequence`) over the live gateway; the same expectations apply
as in `sim` mode.

The test-server binary could not be downloaded in the build sandbox, so **this driver has not
been executed**; `tests/e2e/test_pilot.py::test_pilot_through_the_temporal_test_server` is
`temporal`-marked and additionally opt-in (`PILOT_TEMPORAL=1`) until someone has run it where the
download works. It is the place where a signal-payload regression (GAP-01) would show as a stalled
sequence rather than a recorded contract error.

## Gaps found and fixed (BO-09 follow-up)

The first pilot run surfaced the gaps below. All but GAP-06 (by design) are **FIXED** in the
BO-09 follow-up (no separate commit; "fixed in BO-09 follow-up"). The table keeps the original
finding and records what changed and where the regression test lives.

| id | severity | where | behaviour vs expected | status |
|---|---|---|---|---|
| GAP-01 | high | `activities/conversation.py::_signal_sequence` and `notify_bounce` vs `workflows/investor_sequence.py` `reply` / `opt_out` / `bounce` | The pipeline signalled with `ReplySignal` / `OptOutSignal` / `BounceSignal` payloads; the handlers took no argument, so under the Temporal SDK (`handler(*args)`) the signal raised `TypeError`, failed the workflow task and stalled the sequence after any reply. | **FIXED in BO-09 follow-up.** The payload dataclasses live in one shared module, `wholesaling/signals.py`, imported by both sides; the handlers are `reply(payload: ReplySignal \| None = None)` etc., record the payload in the status (`last_signal`) and treat duplicates as no-ops. Tests: `tests/workflows/test_investor_sequence_definition.py::test_cancel_signal_handlers_accept_the_shared_payload_types`, `tests/pilot/test_sim.py` (the former strict xfail now passes), `tests/e2e/test_pilot.py` asserts zero contract errors. |
| GAP-02 | high | `db/repositories/messages.py::thread_cancel_reason` | An out-of-office stored as `ambiguous` counted as a reply, so `check_cancel_conditions` cancelled the sequence at the next step although A4 flagged `is_auto_reply` and sent no signal (INV-22). | **FIXED in BO-09 follow-up.** `thread_cancel_reason` skips rows whose `meta.a4.is_auto_reply` is true (`is_auto_reply()` helper); `classify_inbound` was verified to never signal `reply` for auto-replies. Test: `tests/activities/test_conversation.py::test_auto_reply_is_ambiguous_and_never_cancels_the_sequence` (OOO -> check continues; real reply -> `reply`); `ooo_then_real_reply` moved from KNOWN_DEFECT to PASS. |
| GAP-03 | high | WF-02 sequence starter | Nothing created the sequence row and started `InvestorSequenceWorkflow` after A2 found a primary email, and nothing ordered priority-tier investors first; the pilot runner played the role. | **FIXED in BO-09 follow-up.** `activities/sequence_starter.py`: `list_startable_investors` (score >= 50, not excluded, market Active/Watch, primary email, not suppressed, no sequence on record; priority tier first), `start_investor_sequence` (row with the deterministic id `investor-sequence:{sequence_id}` via `SequenceRepository.create(sequence_id=...)`, `sequence.started` audit, `start_workflow`; a failed start cancels the row `start_failed` for a later retry), activities `start_investor_sequences(market_id, limit)` and `run_outreach_kickoff(market_id)`; `run_a2` starts the sequence inline (`start_sequence=True`); daily cap from `config/caps.yaml` `outreach_kickoff_daily_cap` else `OUTREACH_KICKOFF_DAILY_CAP` (default 50, in `.env.example`); `OutreachKickoffWorkflow` + the `outreach-kickoff` daily schedule in `workflows/schedules.py`; worker registrations. The pilot now runs the production starter. Tests: `tests/activities/test_sequence_starter.py`. |
| GAP-04 | medium | `agents/a3.py` + `activities/outreach.py` | POL-03a/b at L1 need `qa_verdict == approve`, but no A5 review was ever recorded for A3 drafts, so L1 could never auto-send an initial or follow-up; the pilot wrote a stand-in approval. | **FIXED in BO-09 follow-up.** `decide_and_send` calls `qa_review_draft` (A5 pre-checks + model through the conversation context's runner, persisted with `QaReviewRepository`, `outreach.qa_reviewed` audit, idempotent per row) before `build_policy_context`; the locked CPY-01/CPY-03/CPY-04 copy passes the pre-checks. The pilot stand-in is removed; the L1 persona auto-sends through the real path. Tests: `tests/activities/test_outreach.py` (L1 approve sends, rewrite queues, prohibited claim blocks without a model call, one review across retries). |
| GAP-05 | medium | `activities/conversation.py::draft_and_qa_reply` + `db/repositories/review.py` | Outside a Temporal activity `meta.temporal_workflow_id` was never set and nothing wrote the `inbound.workflow_started` fallback, so a queued reply was unresolvable (422); the pilot wrote the event. | **FIXED in BO-09 follow-up.** `DraftReplyInput.workflow_id` / `FetchMessageInput.workflow_id` carry the id the `GraphInboundMessageWorkflow` passes (`workflow.info().workflow_id`); the activity uses `activity.info()` when inside Temporal, else the argument; `graph_fetch_message` writes `inbound.workflow_started` on the inbound row and `ReviewRepository.resolve_workflow_id` also looks the event up through `meta.reply_to_message_id`. The pilot workaround is removed. Tests: `tests/activities/test_conversation.py::test_workflow_id_passed_by_the_caller_is_resolvable_outside_temporal`, `tests/workflows/test_inbound_pipeline.py::test_workflow_hands_its_id_to_fetch_and_draft`. |
| GAP-06 | low | `agents/a4.py::SENSITIVE_CONTENT_RE` | Any `$` figure or `150k`-style number in a draft flags it sensitive, so an L1 reply that restates the investor's own cap queues. | **By design, left as is**; documented in `docs/agents/conversation.md` ("Sensitive content"). The report keeps the note. |
| GAP-07 | low | A2 / fixtures | A priority investor without an email candidate (Gulf Coast Rentals LLC) got no outreach and nothing surfaced "qualified but unreachable". | **FIXED in BO-09 follow-up.** `run_a2` writes one `investor.unreachable` audit event per qualified investor without an email (investor id, score, priority, A2's reasons); `reporting/kpis.py` counts it as `qualified_unreachable` and the digest shows "qualified but unreachable" (text and HTML). Tests: `tests/activities/test_sequence_starter.py::test_run_a2_records_a_qualified_investor_without_email_as_unreachable`, `tests/reporting/`. |

Determinism: scenario outcomes, row counts, decisions and approvals are identical run to run.
Row UUIDs are generated by the database, and A3 picks the INV-11 subject variant from
`investor_id % 3`, so the variant mix (reported) and therefore prompt sizes and token totals move
by a few tokens between runs; the `seed` only fixes the pilot's own choices (sequence tokens).

Observations that are not defects: the first follow-up of a sequence started on a Tuesday lands
on the following Monday because the 4-day interval falls on Saturday and the send window moves it
(INV-12 working as specified); the bare mock model's schema placeholder would classify an
unscripted message as `interested` (first enum member) with confidence 0, so the pilot installs a
catch-all that answers `ambiguous` / empty extraction / `approve` at 0.5 and lists every such call
in the report (none in the baseline run); costs are the mock provider's token counts priced at the
routed model's rate (`agent_runs.cost_usd`, NUMERIC(12,6)), not a vendor invoice.
