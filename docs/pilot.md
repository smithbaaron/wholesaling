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
M1 (21 seed markets)  ->  A1 + A2 on Houston, TX and Columbus, OH  ->  one InvestorSequenceWorkflow
per contactable persona (priority tier first)  ->  scripted replies injected into the mock Graph
gateway and announced twice through POST /webhooks/graph  ->  GraphInboundMessageWorkflow
(A4 classify -> buy-box versioned upsert -> A4 draft -> A5 QA -> policy engine)  ->  the operator
approves every queued draft through POST /review/{id}/approve one simulated hour later  ->  sends
through the mock gateway in the original conversation  ->  delta-poll replay  ->  report
```

Every piece in that chain is the production code: the activities in `activities/{discovery,
outreach,conversation,graph}.py`, the repositories, `policy/engine.py`, the workflow `run()` loops
in `workflows/investor_sequence.py` and `workflows/graph_maintenance.py`, the webhook route, the
review route (same auth, same `human_decision` signal, same audit events) and the policies route.
The pilot adds three stand-ins, each flagged in the report's gap list:

1. `pilot/sim.py` drives the workflow loops. It swaps the two workflow modules' `workflow` name
   for a shim (the technique of `tests/workflows/test_investor_sequence_sim.py`) so
   `execute_activity` runs the real activity, `wait_condition` parks the workflow until a signal
   or a simulated deadline, and `now()` reads a simulated clock that the Graph and conversation
   activity contexts and the mock Graph gateway share. Exactly one workflow runs at a time and
   ties resolve in registration order, so a run is deterministic. Its `SimTemporalGateway`
   starts workflows by name and delivers signals the way the Temporal SDK does
   (`handler(*args)`, dataclass payloads mapped by field name).
2. The L1 persona's sequence draft gets an A5 `approve` written between `prepare_step` and
   `decide_and_send`, because nothing in the A3 path calls A5 (GAP-04). Without it L1 can never
   auto-send.
3. Inbound reply drafts get an `inbound.workflow_started` audit event, the documented fallback the
   review route uses to find the owning workflow, because `meta.temporal_workflow_id` is only set
   inside a Temporal activity (GAP-05).

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
| `l1_auto_send_then_price_talk` | Lone Star Flips LLC (Houston, 75) | interested reply, then a pricing/contract question | with `investor_initial_outreach` and `investor_reply` scoped to L1: QA-approved initial auto-sends, clean reply auto-sends, price/contract talk queues (POL-03a, POL-03c, POL-09, XC-01) |
| `partial_then_correction` | Heights Holdings LLC (Houston, 71) | partial buy box, then "I said 150k max, not 250k" | version 1 with unknowns, version 2 with `correction: price_max` history (INV-14, INV-21) |
| `two_buy_boxes` | Keller Realty Investments LLC (Houston, 64, agent-investor) | two criteria sets in one email | two keyed version-1 rows (BB-03) |
| `not_interested` | Fast Close Houston LLC (Houston, 61, wholesaler) | decline after touch 2 | no draft, cancel `reply` after 2 touches |
| `prospect_interested_full_buy_box` | Maria Hernandez (Houston, 58, individual) | complete buy box after touch 1 | version 1 with no unknowns; prospect tier |
| `no_contact_found` | Gulf Coast Rentals LLC (Houston, 76) | A2 finds no email | no sequence can start (GAP-07) |
| `bounce_after_touch_1` | Harbor Capital LP 6 (Columbus, 84) | NDR from postmaster, new conversation | `bounce` suppression, `bounce` signal, cancel after 1 touch (INT-25) |
| `opt_out_after_touch_2` | Summit Equity LLC 5 (Columbus, 77) | "Unsubscribe" | `opt_out` suppression at every level, cancel after 2 touches (POL-03l, INV-15) |
| `question` | Prairie Property LLC 9 (Columbus, 68) | asks about sourcing | reply drafted, no buy box invented |
| `not_now_with_date` | Summit Holdings Inc 7 (Columbus, 67) | "circle back January 15, 2027" | `reconnect_date` captured (INV-15) |
| `ooo_then_real_reply` | Harbor Assets Inc 2 (Columbus, 66) | automatic reply, then a real one after touch 2 | auto-reply never cancels (INV-22) - **currently trips GAP-02** |
| `sarcasm` | Blue Capital LLC 0 (Columbus, 65) | "Oh sure, I LOVE spam" | `not_interested`, no draft (INV-22) |
| `quoted_old_text` | Pine Property Trust 3 (Columbus, 62) | reply quoting our email | `quoted_text_stripped`, buy box from the new text only (INV-22) |
| `suppressed_before_start` | River Holdings LLC 4 (Columbus, 56) | address suppressed by hand first | sequence cancelled `suppressed`, nothing drafted (POL-03l) |
| `priority_before_prospect` | cohort | - | within each market, priority-tier sequences start before prospects (INV-04) |

Each persona scenario declares its expected end state (`Scenario.expected(level,
approve_everything, qa_standin)`): sequence status and cancel reason, touches sent and how many
went out without a human, inbound rows, classification sequence, reply drafts / sent / auto-sent,
items that waited in the review queue, buy-box versions per key, corrections, suppression reason,
model calls, single conversation, tier; plus per-message checks (`is_auto_reply`,
`quoted_text_stripped`, `reconnect_date`, the signal sent to the sequence, the QA verdict). The
same catalogue grades an L0, L1, L2 or no-approval run.

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
known defect covers counts as passing; `--strict` makes it fail), 1 when a scenario fails or an
invariant breaks, 2 when `--temporal` cannot start the test server. A run takes about four
seconds of wall time for fourteen simulated days.

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
  only failing checks are the ones its `known_defects` entry names (today: `ooo_then_real_reply`
  for GAP-02). When the defect is fixed the scenario passes and `tests/e2e/test_pilot.py` asks you
  to shrink `KNOWN_DEFECT_SCENARIOS`.
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
  per investor action. A full L0 run yields 12 / 5 / 8 approvals for initial / follow-up / reply,
  so nothing is ready (the bar is 50); the point is that the numbers come out of the same path a
  live L0 month would feed.
- **Signal contract errors**: every signal whose payload the handler could not accept (GAP-01).
- **Gaps**: the defects and missing pieces below, with run evidence.

Baseline L0 run (seed 20261027): 21 markets scored (0 Active, 20 Watch), 21 investor rows in the
two markets, 15 qualified, 5 priority, 14 with an email; 14 sequences (1 completed; cancelled
reply 10, bounce 1, opt_out 1, suppressed 1); 18 touches (13 / 3 / 1 / 1 by step, 1 auto); 14
inbound (1 bounce, 1 auto-reply; buy_box_supplied 6, question 2, not_interested 2, not_now 1,
opt_out 1, ambiguous 1); 6 current buy boxes over 7 versions with 1 correction; suppressions
bounce 1, opt_out 1, manual 1; 9 reply drafts, 9 sent (1 auto), 0 blocked; 25 queue items, 25
approvals, 0 edits; QA approve 10, flag `sensitive_content` 1; 36 model calls = 36 `agent_runs`
(A4 classify 12, extract 6, draft 9; A5 qa 9), $0.0687 at mock pricing; 28 workflows, 0 failed;
11 signal contract errors.

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
- The L1 auto-send path for the sequence relies on the pilot's QA stand-in (GAP-04); the pilot
  proves the policy engine and the send path behave at L1 when the condition holds, not that the
  system can produce the condition.

## Temporal mode (`--temporal`)

`pilot/temporal_driver.py` runs the same scenarios through `temporalio.testing.
WorkflowEnvironment.start_time_skipping()` with the real worker (`workers.main.build_worker`) and
`LiveTemporalGateway` over the environment's client, so the webhook, the review route and the
conversation activities start and signal workflows through Temporal. Time moves with `env.sleep`;
the driver polls the database for newly sent touches, injects replies when due, approves queued
drafts through the route and stops when every sequence is closed or nothing has changed for three
simulated days. The L1 persona's QA stand-in is not possible in this mode (no hook between
activities), so its initial queues like the others; expectations adapt (`qa_standin=False`).

The test-server binary could not be downloaded in the build sandbox, so **this driver has not
been executed**; `tests/e2e/test_pilot.py::test_pilot_through_the_temporal_test_server` is
`temporal`-marked and additionally opt-in (`PILOT_TEMPORAL=1`) until someone has run it where the
download works. Expect it to surface GAP-01 for real: a sequence signalled with a payload fails
its workflow task and never closes, which the stall detector reports.

## Gaps found (for the lead; the pilot does not modify these files)

| id | severity | where | behaviour vs expected |
|---|---|---|---|
| GAP-01 | high | `activities/conversation.py::_signal_sequence` and `notify_bounce` vs `workflows/investor_sequence.py` `reply` / `opt_out` / `bounce` | The pipeline signals with `ReplySignal` / `OptOutSignal` / `BounceSignal` payloads; the handlers take no argument. The Temporal SDK calls `handler(*args)`, so the signal raises `TypeError` inside the workflow and fails the workflow task (retried forever; the sequence stalls after any reply). The existing Temporal e2e tests signal without payloads, so this was never exercised. Fix: handlers accept an optional payload, or the activities send none. Evidence: 11 contract errors per L0 run; `tests/pilot/test_sim.py` carries a strict xfail that flips when fixed. |
| GAP-02 | high | `db/repositories/messages.py::thread_cancel_reason` | An out-of-office is stored as `ambiguous`; the check treats every non-`unrelated` inbound as a reply, so `check_cancel_conditions` cancels the sequence at the next step although A4 flagged `is_auto_reply` and sent no signal. INV-22 says auto-replies never cancel. Fix: skip rows with `meta.a4.is_auto_reply`. Evidence: `ooo_then_real_reply` (known defect): 1 touch instead of 2. |
| GAP-03 | high | WF-02 sequence starter | Nothing creates the sequence row and starts `InvestorSequenceWorkflow` after A2 finds a primary email, and nothing orders priority-tier investors first. `docs/agents/outreach.md` describes it; the pilot runner plays the role. |
| GAP-04 | medium | `agents/a3.py` + `activities/outreach.py` | POL-03a/b at L1 need `qa_verdict == approve`, but no A5 review is ever recorded for A3 drafts, so L1 can never auto-send an initial or follow-up in production. The pilot writes a stand-in approval for one persona. |
| GAP-05 | medium | `activities/conversation.py::draft_and_qa_reply` + `db/repositories/review.py` | The review route resolves an inbound reply's workflow from `meta.temporal_workflow_id` (only set inside a Temporal activity) or an `inbound.workflow_started` audit event nothing writes; outside Temporal a queued reply is unresolvable (422). The pilot writes the event. |
| GAP-06 | low | `agents/a4.py::SENSITIVE_CONTENT_RE` | Any `$` figure or `150k`-style number in a draft flags it sensitive, so an L1 reply that restates the investor's own cap queues. Intended conservatism; noted for draft style. |
| GAP-07 | low | A2 / fixtures | A priority investor without an email candidate (Gulf Coast Rentals LLC) gets no outreach and nothing surfaces "qualified but unreachable". |

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
