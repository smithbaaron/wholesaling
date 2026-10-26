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

---

# Synthetic seller pilot (build order BO-14)

REQUIREMENTS.md BO-14 (mirror of the investor pilot through S1 -> S2 -> S3 -> inbound seller
replies / web form -> S4 -> A7 -> S5 -> human contract -> S6), BO-20, SEL-01..SEL-12, A7-12..17,
POL-03d..k. Code: `wholesaling/pilot/seller_scenarios.py`, `seller_runner.py`,
`seller_report.py`, the `seller` subcommand in `pilot/cli.py`, the seller additions at the end
of `pilot/sim.py`, `GET /pilot/seller-report` in `api/routes/pilot.py`; tests in
`tests/pilot/test_seller_*.py` and `tests/e2e/test_seller_pilot.py`.

## What it is

Seventeen named scenarios drive sixteen seller leads through the whole seller loop **in mock
mode**, on a fresh SQLite database, with no Temporal server, no mailbox, no model, no DocuSign:

```
M1 (21 seed markets) -> the operator makes Houston Active (the PRE-17 approval; Columbus stays
Watch) -> A1 + A2 on Houston (the buyers S6 will blast; three get an operator-entered buy box)
-> S1 + S2 through activities.seller.refresh_market (the weekly refresh, start_sequences=False;
Columbus is refused "market_not_active") -> three hand-entered leads (GAP-S01) -> the production
starter start_seller_sequence, one lead every two simulated minutes -> SellerSequenceWorkflow
(A5 QA on every step, PolicyEngine(seller_outreach_email)) -> scripted seller emails injected
into the mock Graph gateway and announced twice through POST /webhooks/graph ->
GraphInboundMessageWorkflow routes the seller thread to its child SellerInboundWorkflow (S4
classify -> intake merge -> parcel check -> complete | S4 reply -> A5 QA -> policy) -> homeowners
POST the real /sell form (multipart, photos to the mock blob store) -> OfferWorkflow (A7 steps
1-10 with V1..V6, A5 QA, PolicyEngine(offer_send)) -> NegotiationWorkflow (reminders 2/5/10,
expiry 14, re-engagement 60/90) with the seller's accept / counter and the operator's override
-> the operator's clicks on the REAL /deals routes (PSA + DocuSign draft envelope, mark signed
with deadlines, earnest money, buyer response, select buyer, deposit, closed / dead) ->
DispositionWorkflow (S6 blast, T-3d/T-1d/T-0 deadline alerts, dead-deal follow-up) -> collect
-> grade -> SellerPilotReport
```

Every piece in that chain is the production code. The pilot adds:

1. the simulator (`pilot/sim.py`, appended for BO-14): `SellerWorkflowSimulator` (pre-activity
   observers, `execute_child_workflow` for the Graph -> seller hand-off, the parent parks as a
   waiter until the child closes), `patched_workflow_modules_for` over the seven workflow
   modules, `DeferredStartSimGateway` (a workflow starts at the next scheduler turn, as a real
   Temporal dispatch would, so routes that start a workflow before committing read committed
   rows) and `convert_run_args` (start payloads mapped onto the workflow's input dataclass by
   field name, as Temporal's converter does). The investor `patched_workflow_modules` and
   `SimTemporalGateway` are untouched; `tests/pilot/test_sim.py` still passes.
2. `SellerScriptBook`: scripted S4 classify / extract / draft and A5 QA outputs per scripted
   seller message, A5 approvals for the locked templates (four sequence steps, the offer, the
   S5 reminder and re-engagement, the S6 blast), an empty A7 comp-audit verdict (the
   deterministic pre-filters carry V3). Any other model call is listed as unscripted (none in
   the baseline).
3. Four work-arounds it reports as gaps: property-fixture parcels registered with the mock deeds
   provider and A7 fixture subjects aliased under the leads' addresses (GAP-S02), three seeded
   leads (GAP-S01), the S5 router it plays for post-offer replies (GAP-S04), and two engine-level
   injections for one computation each - three drift comps before V6 (`inject_comp`, as the
   engine tests do) and a swapped `engine.compute_mao` so V5 catches a mismatch (GAP-S07).

## The scenarios

| id | persona (source, A7 fixture) | path | proves |
|---|---|---|---|
| `s1_partial_intake_to_dead_deal` | Dolores Ramirez (S1, clean subject aliased) | reply 26 h after touch 1 with motivation + condition; S4 asks timeline + co-owners (L0 queued, L1 auto); second reply completes; offer $88,000 (ARV $240,145, Medium: no photos); seller accepts by email; operator: PSA, signed, earnest requested then deposited; S6 blasts 3 buyers; inspection passes; operator marks the deal dead | SEL-04/06/07, A7-01, SEL-11, SEL-10, WF-06, POL-03i/j (release reminder raised) |
| `s1_full_reply_counter_inside_room` | Thomas Nguyen (S1, tier-2 subject aliased; DNC-listed phone) | everything in one reply; offer $76,000 / MAO $82,104 (Low: Medium less photos -> gate fails, human sends); counter at 60% of the room: L0 human override accepts, L1 S5 auto-accepts; `contract.requested` | SEL-03, CMP-02, SEL-09, POL-03h; **GAP-S09** |
| `s1_counter_above_mao_human_declines` | Gerald Pike (S1, clean aliased) | everything in one reply; offer $92,000 auto at L1; counter at 160% of the room -> human; operator declines; lead requeued 90 d later | SEL-09, POL-03h, POL-06 |
| `s1_offer_expires_then_reengage` | Linda & Mark Okafor (S1, sqft-gap subject aliased) | V1 third source agrees with the county (`adjusted`); offer $81,000; no answer: reminders 2/5/10, expiry 14, re-engagement 60/90 | A7-12, SEL-09, WF-05; **GAP-S05** at L0 |
| `seed_no_reply` | Quiet Owner (seeded) | 4 touches at 3/7/14 d, completed | SEL-04, WF-03 |
| `seed_hostile_opt_out` | Hostile Owner (seeded) | "take me off your list" 1 h after touch 1 | opt_out suppression, lead suppressed, cancel `opt_out`, no draft (POL-03l) |
| `seed_asks_for_a_number` | Price Asker (seeded) | "what's your offer?" at 2 h (draft **blocked**: cadence), again at 30 h: S4 asks two fields, `asks_offer` -> sensitive -> human at every level | POL-06, POL-09, SEL-07; **GAP-S08** |
| `web_form_full_to_close` | Pat Doe (web form, clean subject, 1 photo) | complete form -> offer $88,500 (High) -> human sends (gate `no_open_flags`) -> accepts -> PSA -> signed -> blast 3 -> buyer response, select buyer ($10,000 fee, $5,000 deposit), earnest deposited, deposit, closed with the realised fee | SEL-12, SEL-08, A7-09, SEL-11, SEL-10, BB-11; **GAP-S03**, **GAP-S11** |
| `web_form_three_way_conflict` | Lee Conflict (61 Elm Loop) | V1 no majority -> escalate -> Low -> gates fail -> human | A7-12, A7-04, A7-08 |
| `web_form_dirty_comps` | Sam Cedar (930 Cedar Bend) | V3 removes 4 comps, V4 Medium -> Heavy + foundation contingency; offer $40,500 < 35% ARV -> human | A7-14, A7-15, A7-08 |
| `web_form_avm_gap` | Ana Birch (2201 Birch St, Light, 2 photos) | V2 re-runs at tier 2 (7.4% gap), offer $149,500 | A7-13; **GAP-S03** |
| `web_form_pre_send_drift` | Will Willow (717 Willow Way) | three $170/sqft sales injected before V6: ARV +5.7%, MAO/offer/V5/draft recomputed ($101,000), second V6 passes | A7-17, A7-16; **GAP-S03** |
| `web_form_v5_mismatch` | Vera Five (clean aliased) | `compute_mao` returns offer + $500 for this computation: V5 fails, offer blocked, `a7.recompute_mismatch` alert, nothing sent | A7-16 |
| `web_form_photos_missing` | Noah Photo (clean aliased, no photo) | same facts as Pat Doe without photos: High -> Medium | SEL-06; **GAP-S03** |
| `web_form_probate_without_authority` | Pat Probate (clean aliased) | "inherited ... still in probate" with no authority: flag -> gate fails -> human | A7-08 |
| `web_form_same_parcel_merges` | Dolores Ramirez again (web form at day 45) | the form merges into the S1 lead (two distress rows were already one lead): one lead per address, one intake, `lead_created=False` | SEL-02, SEL-12 |
| `market_not_active_refused` | cohort | `refresh_market` on Columbus (Watch) | `refused_reason=market_not_active`, nothing queued (SEL-01, M1-11) |

Each persona declares its expected end state (`SellerScenario.expected(level)`): lead status and
parcel verification, sequence status / cancel reason / touches (auto), inbound rows and
classifications, S4 replies (drafted / sent / auto / sensitive / queued / blocked), intake
completion and photo link, offer status / ARV confidence / failed gates / open flags /
verification passes and per-pass results / sent / auto, counters (within room, decision, who
accepted), negotiation outcome, reminders and re-engagements (timeline and rows actually sent),
requeue, contract status, blast recipients (auto), buyer, deposit, realised fee, dead reason,
earnest-release reminder, suppression, lead dedupe. `known_defects` names the checks a documented
gap makes fail; a scenario failing only on those is `known_defect`.

## Running it

```bash
uv run wholesaling-pilot seller                     # L0, Houston, SQLite in memory
uv run wholesaling-pilot seller --level L1 --out seller.md --json seller-pilot-report.json
uv run wholesaling-pilot seller --no-approve        # the operator approves and clicks nothing
uv run wholesaling-pilot seller --db sqlite+aiosqlite:///seller.db
```

Exit status 0 when every scenario passes or fails only on a documented gap (`--strict` fails
those too; today every run is `PASS (with known defects)`), 1 on a failure or a broken
invariant. A run takes about seven seconds of wall time for 93 simulated days.
`GET /pilot/seller-report?format=json|text|markdown` (operator auth) serves the newest report:
the `seller_pilot.completed` audit event when the API shares the pilot's database, else the JSON
file at `SELLER_PILOT_REPORT_PATH` (default `seller-pilot-report.json`).

## Reading the report

- **Invariants**: A7-18 every offer has V1..V5 rows and every sent offer a V6 row; A7-16 V5
  matches unless injected; POL-03i/j no automated actor wrote a contract / earnest-money event and
  every signed contract names a human signer; SEL-05 identity + postal address + opt-out on every
  sent seller message, and (known defect GAP-S12) the canonical TX disclosure; POL-03e / RUL-06
  no SMS row and no SMS policy decision; INV-24 one inbound row per injected email though the
  webhook was called twice; SEL-12 one `web_form` row per submission; RUL-05 one mock send per
  sent row; XC-02 one `agent_runs` row per model call; SEL-02 one lead per normalised address;
  POL-03l no send to a suppressed seller; no workflow crashed or was left waiting without a
  timeout. An invariant whose `note` starts with `known defect` is listed but does not fail the
  run (`ok_strict` does).
- **Aggregates**: S1 counts and refusals, leads by source / status, S2 trace / DNC, sequences,
  touches, inbound and form rows, classifications, S4 replies, intakes, offers by status and
  confidence, failed-gate histogram, verification rows by `pass:result`, offers sent / auto /
  queued / blocked, counters, accepted / declined / expired, reminders, contracts, blasts, buyers,
  deposits, realised fees, suppressions, queue and approvals (and approval **failures**), QA,
  policy decisions by `action:decision`, model calls, SMS rows, workflows.
- **POL-04 readiness** for the five seller actions over the whole run (the window is widened
  from 30 days to the run's length because the audit rows carry wall-clock timestamps).
- **Gaps**: GAP-S01..S12 below, each with the run's evidence.

Baseline L0 run (seed 20261027, 93 simulated days): 1 Active market; S1 pulled 11 records / 10
parcels, created 5 leads (rejected: no_value 1, not_residential_1_4 2, outside_deal_band 1,
suppressed 1), Columbus refused; S2 traced 5, reachable 4, unreachable 1 (Boyd estate), DNC-listed
2; 16 leads (5 distress, 3 seeded, 8 web form); 7 sequences (1 completed; cancelled reply 5,
opt_out 1); 10 touches, 0 auto; 12 emails injected = 12 rows, 9 forms = 9 rows; classifications
interested 20, opt_out 1; S4 replies 3 drafted / 2 sent / 2 sensitive / 1 blocked; 12 intakes
complete, 2 photo links; 12 offers (accepted 3, declined 1, expired 7, blocked 1), confidence
High 4 / Medium 6 / Low 2; gates failed no_open_flags 8, arv_confidence 2, min_pct_of_arv 1;
3 offers with every gate passed; verification rows V1 pass 10 / adjusted 1 / escalate 1, V2 pass
11 / adjusted 2, V3 pass 12 / adjusted 1, V4 pass 11 / adjusted 1, V5 pass 12 / fail 1, V6 pass
21 / adjusted 1; 11 offers sent (0 auto, 11 queued), 1 blocked; 2 counters (both to a human);
reminders 0 sent / 21 queued, re-engagements 0 (GAP-S05), 35 failed approvals; 1 lead requeued;
3 `contract.requested`, 2 contracts signed, 1 closed ($10,000 realised), 1 dead; 6 blast drafts,
6 sent (0 auto); 1 buyer, 1 deposit; 64 queue items, 29 approvals; 104 model calls = 104
`agent_runs` (A5 65, A7 comp audit 13, S4 classify 12 / extract 11 / draft 3), $0.1757 at mock
pricing; 65 workflows, 0 failed, 0 parked; 0 signal contract errors. At L1 the same run
auto-sends all 10 touches, 1 of 2 intake replies (the price question stays human), 3 of 11 offers
(the three with every gate passed), S5 auto-accepts the in-room counter, sends 3 reminders and 2
re-engagements for that offer and auto-sends all 6 blasts; 9 approvals, 30 failed.

## What it does not prove

Everything in the investor section applies (no mailbox, no model, no Temporal server, no
production fixtures). In addition: no real property data (the A7 fixtures are seven hand-made
subjects; the pilot aliases them under the leads' addresses, GAP-S02); DocuSign is the mock
(envelopes are "created" drafts, nothing is signed); the blob store is in memory; the S5 router
for post-offer replies does not exist yet, so the acceptance / counter emails go through the
inbound pipeline (which does nothing with them) and the pilot signals `NegotiationWorkflow`
itself (GAP-S04); A5's judgement of the seller templates, the offer, the reminders and the blast
is scripted (approve 0.93-0.96) - whether the real model approves them is a BO-13 eval question.

## Gaps found (BO-14)

All **OPEN**; the pilot works around each one inside `wholesaling/pilot/` and reports it. The
`known_defects` entries in the catalogue and `tests/e2e/test_seller_pilot.py` name the ones
that make a check fail today, so fixing a gap flips a check and the lists must be updated.

| id | severity | where | behaviour vs expected | evidence / work-around |
|---|---|---|---|---|
| GAP-S01 | low | `integrations/data/fixtures/{distress,skiptrace}.json` | The Houston distress fixture yields four reachable owners; the seller pilot needs seven sequence personas (no-reply, opt-out, price-asker on top). Expected: enough fixture rows. | three leads seeded by hand (`source=pilot_seed`), started through the production starter |
| GAP-S02 | medium | `fixtures/properties.json` vs `distress.json` / `deeds.json`; `mock.MockDeedRecordsProvider.resolve_parcel`, `property.MockPropertyDataProvider` | The seven A7 fixture subjects are unknown to the deeds mock, so a web form on them never parcel-verifies (S4 keeps asking for the address, no offer); no S1 distress address has a property fixture, so A7 blocks every S1 lead with "property could not be resolved". Expected: one coherent mock world. | `register_parcel` adds the fixture parcels to the deeds mock; `alias_subject` copies a fixture subject under the lead's address (parcel ids suffixed, comps re-zipped) |
| GAP-S03 | medium | `api/routes/sell.py` (`TEXT_FIELDS`), `activities/seller.py::seller_intake`, `a7/gates.py` | The form has no co-owner / liens / HOA question and S4 asks optional fields only while a required one is missing, so a complete form intake reaches A7 with `co_owners_unknown=True` -> `co_owner_missing` -> gate `no_open_flags` fails -> every form-sourced offer queues for a human even at L1. Expected: the form or S4 asks the SEL-06 optional questions once. | 8 of 12 offers fail `no_open_flags`; `web_form_full_to_close`, `web_form_avm_gap`, `web_form_pre_send_drift`, `web_form_photos_missing` are `known_defect` |
| GAP-S04 | high | `activities/seller.py` (`seller_classify_inbound` / `seller_intake`) vs `workflows/negotiation.py` | A seller's reply after the offer (accept, counter, decline) runs through `SellerInboundWorkflow` as an intake message; the intake is complete, so nothing is drafted and `NegotiationWorkflow` never hears of it - no code path signals `counter` / `accept` / `decline` from inbound mail. Expected: post-offer replies classified and signalled to `negotiation:{offer_id}` (SEL-09). | the pilot injects the email (to show the no-op) and then signals S5 itself (`_play_negotiation_move`) |
| GAP-S05 | high | `workflows/negotiation.py` (no `human_decision` signal) vs `activities/offers.py::send_negotiation_message` | At L0 the S5 reminders and re-engagements queue with `meta.temporal_workflow_id=negotiation:{offer_id}`; the review route resolves it and signals `human_decision`, which the workflow does not define -> 502 (`has no signal 'human_decision'`), later `already completed`; no reminder can ever be approved and the queue fills with 35 unresolvable items per run. Expected: the workflow accepts `human_decision` (send via a `send_approved` activity) or a child workflow sends them. | `s1_offer_expires_then_reengage` is `known_defect` at L0 (`reminders_delivered` 0 of 3); `approval_failures` 35 |
| GAP-S06 | medium | `api/routes/deals.py::mark_signed` | The route starts `DispositionWorkflow` before committing the signed status; under a real Temporal server the worker's `load_contract` can read `sent_for_signature` and finish `not_signed` (no blast, no alerts). Expected: commit first, then start (as `/sell` does). | by code inspection; not reproducible on SQLite (shared connection) and the pilot gateway dispatches at the next turn |
| GAP-S07 | low | `agents/a7/engine.py` (`run_v5`, `compute_mao`) | No seam to exercise a V5 mismatch. Expected: an injection hook. | the pilot swaps `engine.compute_mao` for one computation, like `tests/agents/a7/test_engine.py` |
| GAP-S08 | high | `agents/a5.py::run_prechecks` (`cadence_violation`) <- `activities/seller.py::seller_intake` | A seller who answers within 24 h of our note gets no reply: the pre-check flags the S4 reply because *our* last outbound on the thread is < 24 h old, the BLOCK verdict becomes a blocked draft, the inbound workflow ends, nothing retries later; the sequence was already cancelled on `reply`, so the lead goes dark. The same rule applies to A4 investor replies (the investor pilot scripted >= 26 h delays). Expected: an answer to a newer inbound is not a cadence violation, or the send is deferred rather than blocked. | `seed_asks_for_a_number` is `known_defect` (`s4_blocked` 1); the first draft of every other persona would have been blocked with a sub-24 h reply, so the catalogue scripts 26-30 h |
| GAP-S09 | medium | `agents/a7/gates.py::derive_open_flags` / `_NEGATIVE` | `co_owners` text such as "sole owner" or "just me" is read as unidentified co-owners (only no/none/n/a count as negative), so `co_owner_missing` fires for a seller who owns alone. Expected: sole-ownership statements clear the flag (or S4 normalises them). | `s1_full_reply_counter_inside_room` is `known_defect`; the other S1 personas script `co_owners="none"` |
| GAP-S10 | low | `api/routes/sell.py` (`datetime.now`), `api/routes/deals.py::_now` | The form stamps `received_at` / `submitted_at` and the deals routes stamp buyer responses and defaults with the wall clock, not an injectable clock, so form rows sit out of order on a simulated timeline and every date must be passed explicitly. Expected: routes read the clock the activities read. | pilot passes every /deals date |
| GAP-S11 | low | `db/repositories/contracts.py` (`mark_signed` / `mark_closed` / `mark_dead`) | Nothing moves the seller lead past `offered`: signing, closing and death leave `seller_leads.status` unchanged although the enum has `under_contract` / `closed` / `dead` and `handoff_to_s5` / `requeue_seller` guard on them. Expected: `mark_signed -> under_contract`, `mark_closed -> closed`, `mark_dead -> dead`. | `web_form_full_to_close` is `known_defect` (`lead_status` offered) |
| GAP-S12 | medium | `agents/a7/templates.py::TX_DISCLOSURE` vs `agents/templates/seller_outreach.py::TX_EQUITABLE_INTEREST_DISCLOSURE` | The offer, reminder and re-engagement emails append A7's own disclosure wording, not the SEL-05 block counsel reviews (PRE-16) and `missing_compliance_blocks` checks; two different disclosures reach sellers from one mailbox. Expected: one disclosure constant rendered by `seller_outreach`. | invariant "canonical TX disclosure" fails for every sent offer / reminder / re-engagement, listed as a known-defect invariant |

Observations that are not defects: the first seller touch goes out at 11:00 Chicago although the
pilot starts at 10:00 (the seller send window opens an hour later than the investor one); V2 on
the AVM-gap subject records two `adjusted` rows (the tier-2 re-run and the 7.4% result) rather
than `adjusted, pass`; the engine prices with the simulated day, so ARVs move by a few dollars
with the day an offer is computed (`$240,116` to `$240,153` for the clean subject) and L0/L1
runs differ by the hour of approval; `NegotiationResult.reminders_sent` lists the days a
reminder was *attempted* (a queued reminder counts), which is why the report also counts rows
actually sent.
