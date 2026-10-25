# A3 investor outreach sequences (build order BO-07 part B)

A3 turns a qualified investor contact (from A2) into four emails in **one Outlook conversation**:
the approved initial (CPY-01) and three follow-ups about four days apart (CPY-03, OPN-04). Every
send goes through the policy engine; a reply, opt-out, bounce or suppression cancels everything
that remains (INV-10, INV-23, WF-21). No model is involved anywhere in this path: the copy is
locked, A3 only fills `{first_name}`, `{market}` and `{sender_name}`.

| Piece | Module | Role |
|---|---|---|
| Templates | `wholesaling/agents/templates/investor_outreach.py` | `INITIAL_BODY` (CPY-01 verbatim), `INITIAL_SUBJECTS` (3 INV-11 variants), `FOLLOWUP_BODIES` (3 drafts), `render`, `footer` |
| Agent | `wholesaling/agents/a3.py` | `A3Outreach.draft(step, ...)` -> `OutreachDraft`; deterministic subject variant; `sequence_plan()` from `config/sequences.yaml` |
| Repositories | `wholesaling/db/repositories/sequences.py`, `messages.py` | sequence rows; outbound draft rows keyed on the idempotency key; `count_sends_today`; cancel-condition lookups |
| Activities | `wholesaling/activities/outreach.py` | `prepare_step`, `decide_and_send`, `send_approved`, `check_cancel_conditions`, `send_window`, `update_sequence` |
| Workflow | `wholesaling/workflows/investor_sequence.py` | `InvestorSequenceWorkflow` (signals, durable sleeps, send window) |
| Settings | `SENDER_NAME`, `SENDER_COMPANY`, `SENDER_POSTAL_ADDRESS`, `OPT_OUT_TEXT`, `SEND_WINDOW_START_HOUR`, `SEND_WINDOW_END_HOUR`, `SEND_WINDOW_WEEKDAYS_ONLY` | CAN-SPAM footer and INV-12 window (approved under PRE-02 / PRE-16) |

## Sequence of one run

```
A2 (qualified contact)
  |  SequenceRepository.create(kind=investor, investor_id, temporal_workflow_id)
  |  client.start_workflow("InvestorSequenceWorkflow", InvestorSequenceInput(...),
  |                        id=investor_sequence_workflow_id(sequence_id))
  v
InvestorSequenceWorkflow.run                       activities (all I/O)            Graph / DB
  for step in 0..3:
    |-- check_cancel_conditions(sequence_id) ----> thread inbound? suppression? paused?
    |      reply/opt_out/bounce/suppressed  ====> update_sequence(cancel) -> return
    |      paused                           ====> hold until `resume` signal
    |-- send_window(market) -----------------> markets.timezone + SEND_WINDOW_* -> seconds
    |      > 0: durable sleep until the window opens
    |-- prepare_step(sequence_id, step) ------> A3 renders template + footer
    |                                           messages row: draft=True,
    |                                           external_message_id = seq:{id}:step:{n}
    |-- decide_and_send(message_id, step) ----> PolicyEngine.decide(action, PolicyContext)
    |      send             ================> Graph create_draft (+conversation id) + send_draft
    |      queue_for_human  ================> row.policy_decision = queue_for_human
    |            wait for signal `human_decision(step)`
    |              approved -> send_approved(message_id, edited_body) -> Graph draft + send
    |              rejected -> update_sequence(cancel "human_rejected:step:n") -> return
    |      blocked          ================> update_sequence(cancel "blocked:<reason>") -> return
    |-- update_sequence(step_done=n, next_send_in_seconds)
    |-- durable sleep intervals_days[n] (steps 0..2), interrupted by reply/opt_out/bounce/pause
  update_sequence(completed=True)
```

Policy actions: step 0 is `investor_initial_outreach` (POL-03a), steps 1..3 are
`investor_followup` (POL-03b). The `PolicyContext` is filled by `build_policy_context`:

| Field | Source |
|---|---|
| `sends_today` | `MessageRepository.count_sends_today(action, market_tz)` - sent rows since local midnight in the market's zone |
| `initial_was_approved` | follow-ups only: the step-0 row of the same sequence has `sent_at` (it went out, auto or human-approved) |
| `recipient_suppressed` | `suppression` table on the normalised recipient |
| `qa_verdict` / `qa_confidence` | latest `qa_reviews` row for the message (`messages.qa_review_id` first). BO-07c writes it; absent means `None`, which the engine treats conservatively (L1 queues) |

At **L0** every step queues for a human and nothing reaches Graph. At **L1** the initial sends
only with a QA approval and under the daily cap; a follow-up sends only if the initial went out
and it is QA-approved. At **L2** both auto-send. Suppression blocks at every level, including
after a human approval (`send_approved` re-checks it, POL-03l).

## Signal contract (what the review queue codes against)

Workflow type: `InvestorSequenceWorkflow`. Workflow id: `investor-sequence:{sequence_id}`
(`wholesaling.workflows.investor_sequence.investor_sequence_workflow_id`). Also exported from
`wholesaling.workflows`.

| Signal | Payload | Effect |
|---|---|---|
| `human_decision` | `HumanDecision` dataclass (below) | approve / edit / reject the queued draft for one step |
| `reply` | none | cancel remaining steps, `cancel_reason="reply"` |
| `opt_out` | none | cancel remaining steps, `cancel_reason="opt_out"` |
| `bounce` | none | cancel remaining steps, `cancel_reason="bounce"` |
| `pause` | none | hold before the next send; the remaining sleep is preserved |
| `resume` | none | release a pause |

```python
@dataclass(slots=True)
class HumanDecision:
    step: int | None  # 0..3; the step the operator acted on
    approved: bool  # False = reject -> sequence cancelled ("human_rejected:step:n")
    edited_body: str | None = (
        None  # replaces the body; the CAN-SPAM footer is re-appended if missing
    )
    actor_id: str | None = None  # operator username, recorded in the audit event
    message_id: str | None = (
        None  # optional: messages.id; used to resolve the step when step is None
    )
```

Temporal serialises the payload as a JSON object keyed by field name and ignores unknown fields,
so `wholesaling.db.repositories.review.HumanDecision` (the API side's dataclass) is compatible:
its `message_id` and nullable `step` are both accepted. A decision for a step that already went
out is ignored; before that the latest signal wins. A decision whose `step` is `None` and whose
`message_id` is not the draft the workflow is waiting on is counted in `status.unmatched_decisions`
and otherwise ignored.

Query `status` returns `InvestorSequenceStatus` as a dict: `sequence_id`, `state`
(`pending | waiting_send_window | deciding | awaiting_human | sleeping | paused | done | cancelled`),
`step`, `steps_sent`, `paused`, `cancel_reason`, `remaining_seconds`, `decisions`
(`{"0": "send", "1": "queue_for_human", ...}`), `pending_human_step`, `pending_message_id`,
`unmatched_decisions`.

Result: `InvestorSequenceResult(sequence_id, steps_sent, cancel_reason, completed, decisions)`.

Row ownership: the workflow's activities write `messages.policy_decision`, `body`, `sent_at` and
`meta.human_approved` when they act on a decision. If the review queue also flips the row, it
should do so *before* signalling (or leave the flip to the workflow) so a later write cannot
overwrite the footer-bearing body the workflow sent.

## Idempotency and restart safety (WF-22, XC-03)

- The draft row's `external_message_id` is `seq:{sequence_id}:step:{n}`. `prepare_step` retried
  returns the same row; `decide_and_send` on a row with `sent_at` returns `sent` without touching
  Graph; `send_approved` on a sent row is a no-op.
- The Graph draft id lives in `messages.meta.graph_draft_id`; a second attempt reuses it. The
  send goes through `activities/graph.send_draft`, which never sends a row twice (RUL-05).
- Audit events are keyed `out:{key}:prepared`, `out:{key}:decided`, `out:{key}:approved`,
  `out:{key}:sent`, `seq:{id}:cancelled`, `seq:{id}:completed`, so a replay logs each once.
- All workflow timestamps come from `workflow.now()`; the durable sleep is the HelloWorkflow
  pattern (wait_condition with timeout), so a worker restart resumes with the remaining time.

## Threading (INV-23)

Step 0 creates a new message; its `conversationId` is stored on `threads.external_thread_id`.
Steps 1..3 are created with that `conversation_id`, so the gateway replies to the latest message
in the conversation (`createReply`, milestone-2 decision 3) and Outlook shows one thread. A
follow-up whose thread has no conversation id (initial never sent) cannot be sent and raises;
the policy normally prevents this because an unsent initial means `initial_was_approved=False`.

Inbound replies arrive through the webhook / delta poll (`upsert_inbound`) and land on the same
thread by `conversationId`. `check_cancel_conditions` reads that thread before every step, so a
reply cancels the sequence even if nothing signalled the workflow; the `reply` signal is the fast
path for the inbound workflow (BO-07c) to use.

## Template approval status and PRE-16

| Template | Row | `approval_status` |
|---|---|---|
| `investor_initial` | CPY-01 | `approved` (verbatim, locked) |
| `investor_followup_1`, `_2` | CPY-03 | `pending_approval` |
| `investor_followup_3` | CPY-04 (tone verbatim) | `pending_approval` |

`approval_status` is a constant on each `Template` and is copied into
`messages.meta.template_approval_status` on every draft, so the review queue can show which copy
is still unapproved. It does **not** change what the policy engine decides: at L0 everything
queues anyway, and at L1 a follow-up also needs a QA approval. When PRE-16 clears the follow-ups,
flip `approval_status=PENDING_APPROVAL` to `APPROVED` on the three `FOLLOWUP_BODIES` entries in
`investor_outreach.py` (and adjust the text to whatever was approved); `tests/agents/test_a3.py`
pins the current status, so the test moves with the decision. Changing `INITIAL_BODY` is a spec
change (CPY-01 says "retained unless you explicitly change it").

Subject variants (INV-11): the initial's subject is `INITIAL_SUBJECTS[investor_id.int % 3]`,
recorded as `meta.subject_variant`, so response rates can be compared per variant without the
operator ever being shown a list to pick from.

## Compliance guards

- **XC-07**: `render` refuses any parameter value or rendered text containing a phrase from
  `PROHIBITED_PHRASES` ("under contract", "we have", "exclusive", "inventory", ...).
- **CMP-01**: `footer(SenderIdentity)` requires name, company, a postal address and an opt-out
  instruction; `A3Outreach` refuses to construct without them, so no draft can exist before
  PRE-02 is cleared and `SENDER_POSTAL_ADDRESS` is set. `send_approved` re-appends the footer to
  an edited body that dropped it.
- **INV-12**: `send_window` uses the market's `markets.timezone` (default `America/Chicago`) and
  `SEND_WINDOW_START_HOUR`/`END_HOUR` (default 09-17, weekdays). Daily caps come from the
  `policies.daily_cap` column (OPN-06 default in `config/caps.yaml`) and count the market's local
  calendar day.

## Tests

- `tests/agents/test_a3.py` - CPY-01 character-for-character, XC-07 denylist, footer rules,
  variant assignment, drafts.
- `tests/db/test_sequences_repo.py` - repositories incl. the `count_sends_today` timezone boundary.
- `tests/activities/test_outreach.py` - activities against SQLite + `MockGraphGateway`: L0 all
  queued; L1 QA-approved under cap sends; cap reached queues; suppressed blocks; reply cancels;
  retries never resend; four steps land in one conversation.
- `tests/workflows/test_investor_sequence_definition.py` - signal/query names, payload
  compatibility with the review queue, worker registration.
- `tests/workflows/test_investor_sequence_sim.py` - the run loop driven by a fake `workflow`
  module (no server): ordering, cancellation points, pause, send window.
- `tests/workflows/test_investor_sequence_workflow.py` - time-skipping e2e (`temporal` marker,
  runs in CI): scripted approvals at L1 produce four messages in one conversation; a reply after
  step 1 cancels steps 2-3 (INV-23); DB-side reply, pause + opt-out, rejection, edited body.
