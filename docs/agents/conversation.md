# Conversation agents (A4, A5) and the inbound pipeline — build step BO-07 part C

REQUIREMENTS.md INV-13..19, INV-21, INV-22, INV-24, WF-19, WF-21, POL-03c, POL-03l, XC-01, XC-02,
XC-07. Code: `wholesaling/agents/a4.py`, `agents/a5.py`, `agents/prompts/{a4,a5}.py`,
`activities/conversation.py`, `workflows/graph_maintenance.py` (`GraphInboundMessageWorkflow`),
`db/repositories/qa_reviews.py`.

## Pipeline (WF-19 / INV-16)

```
Graph webhook / delta poll
  -> GraphInboundMessageWorkflow (id = sha256(subscription, graph id): duplicate notification = no-op)
  -> fetch_message            one messages row per internetMessageId (INV-24)        [stage: fetching]
  -> bounce?  notify_bounce   signal `bounce` to the investor sequence, stop
  -> classify_inbound         A4 classify; opt-out -> suppression row (auto at every level, POL-03l)
                              substantive -> signal `reply` / `opt_out` to the sequence  [classifying]
  -> interested | buy_box_supplied?
       extract_and_upsert_buy_box   A4 extract -> BuyBoxRepository.create_version per box
                                    audit buy_box.created / buy_box.updated (-> D1, BO-08) [extracting_buy_box]
  -> reply warranted (interested, buy_box_supplied, question, not_now, ambiguous)?
       draft_and_qa_reply     A4 draft -> A5 QA -> PolicyEngine.decide(INVESTOR_REPLY)
                              send: create_reply_draft + send in the same conversation
                              queue_for_human: Graph draft left in Drafts, row policy_decision=queue_for_human
                              blocked: local row only, nothing in the mailbox              [drafting]
  -> queued?  wait_condition(human_decision signal, timeout 14 d)                        [awaiting_human]
       apply_human_decision   approved -> send (edited body = new reply draft) / rejected -> discard
  -> done
```

`unrelated`, `not_interested`, `opt_out` and automatic replies get no draft. Every activity is
idempotent per `message_id`: a Temporal retry or a replayed workflow finds the rows a previous
run left (`messages.classification`, `buy_boxes.source_message_id`, the reply row keyed
`reply:<inbound id>`, audit idempotency keys) and does not call the model, Graph or Temporal
again. The `status` query returns `{"state": <stage>, "result": InboundPipelineResult}`.

The workflow result `InboundPipelineResult` keeps the fetch fields flat (`found`, `created`,
`message_id`, ...) and adds `stage`, `classification`, `signalled`, `buy_box_versions`,
`draft_message_id`, `qa_verdict`, `policy_decision`, `sent`, `human_decision`.

### Dependency context

`activities.conversation.get_conversation_context()` builds its context from the Graph activity
context (same session factory, settings and clock) plus `build_runner(settings,
overrides=a4.MODEL_OVERRIDES)` and `get_temporal(settings)`. Tests and the synthetic pilot call
`set_conversation_context(ConversationContext(...))` with the mock runner and mock Temporal
gateway. `prompt_values` (default `{"company": "our team"}`) renders `{company}` into the A4/A5
system prompts; set it to the operator's entity name at deploy time.

## A4 Reply & Buy Box (`A4ReplyAgent`, agent id A4)

| Task | Model | Output |
|---|---|---|
| `classify(thread_messages)` | **Sonnet** via per-agent override `MODEL_OVERRIDES = {"A4": {"classify": "claude-sonnet-5-5"}}` (section 5 lists A4 as Sonnet; routing is by task kind, see docs/agents/llm.md) | `A4Classification{classification, confidence, rationale, is_auto_reply, quoted_text_stripped, reconnect_date?, run_id?}` |
| `extract_buy_box(thread_messages, current)` | Haiku (default for `extract`) | `BuyBoxExtraction{buy_boxes: [ExtractedBuyBox{buy_box_key, criteria, unknowns, corrections, raw_text}], unknowns, corrections, run_id}` |
| `draft_reply(thread, classification, buy_box, missing_fields)` | Sonnet (default for `draft`) | `ReplyDraft{subject, body, asks_for, contains_sensitive, run_id}` |

Classes (INV-13): `interested`, `buy_box_supplied`, `question`, `not_now`, `not_interested`,
`opt_out`, `ambiguous`, `unrelated` (`db.models.enums.MessageClassification`).
Substantive = every class except `unrelated`; automatic replies are never substantive.
Reply warranted = `interested`, `buy_box_supplied`, `question`, `not_now`, `ambiguous`.

Deterministic steps around the model:

- **Quote stripping** (`strip_quoted_text`): cuts at the first Outlook marker
  (`-----Original Message-----`, the `________` separator followed by `From:`, a `From:` line with
  `Sent:`/`Date:` within four lines) or Gmail marker (`On <date> ... wrote:` on one or two lines,
  the first `>` quoted line). If the cut would leave nothing, the body is returned untouched.
  Signatures are not touched. Applied to the latest inbound message before classification and
  to inbound history when rendering the thread.
- **Auto-reply detection** (`detect_auto_reply`): `Auto-Submitted` other than `no`,
  `X-Auto-Response-Suppress`, an `Automatic reply:` / `Out of office` subject, or out-of-office
  phrasing in the body -> `ambiguous` with `is_auto_reply=True`, confidence 1.0, **no model
  call**. The model may also set `is_auto_reply`, which forces `ambiguous`. Auto-replies never
  cancel a sequence (INV-22).
- **Reconnect date** (INV-15): kept only for `not_now`, parsed as ISO `YYYY-MM-DD`; anything
  else becomes `None`.
- **No invention** (INV-14): `EXTRACT_SCHEMA` has no defaults and every criterion is nullable
  with `null` as the first `anyOf` option, so an unscripted mock returns an empty extraction and
  a schema-following model leaves unknowns null. Boxes with no concrete criterion are dropped.
- **Unknown tracking**: `TRACKED_BUY_BOX_FIELDS = geography, asset_types, price_max, condition,
  financing, closing_timeline`. After merging the stated fields over the current version, every
  tracked field still empty is listed in `unknowns` and stored in `buy_boxes.unknown_fields`.
  `draft_reply` receives these as `missing_fields` and `asks_for` is clamped to that list.
- **Corrections** (`compute_corrections`): fields stated now that differ from the current version
  (`{field, old, new}`), plus any the model reported for fields the comparison could not see.
  The repository call uses `mode="merge"` so a correction touches one field and keeps the rest;
  the superseded version stays as history (`is_current=False`, `change_note="correction: ..."`).
- **Multiple buy boxes** (BB-03): one `ExtractedBuyBox` per distinct criteria set, keyed by the
  model's `buy_box_key` slug (`default` for a single box). `BuyBoxRepository.get_by_source_message`
  takes the key so the replay guard works per box.
- **Sensitive content** (POL-03c): `contains_sensitive` is the model's flag OR a regex scan for
  price / offer / contract / earnest / legal / attorney / `$` amounts / `150k`-style figures.
  **By design** (pilot GAP-06): any `$` figure or `150k`-style number in a *draft* flags it
  sensitive even when it merely restates the investor's own cap, so such an L1 reply queues for a
  human instead of auto-sending. The conservatism is intentional - price talk is the one place
  POL-03c wants a human - and the cost is one extra review per figure-bearing reply; the remedy
  is draft style (acknowledge the criteria without repeating the number), not a looser regex.

## A5 QA (`A5QaAgent`, agent id A5, task kind `qa`, Sonnet)

Input `QaInput{draft: QaDraft{to, subject, body, asks_for, contains_sensitive}, thread, facts,
current_buy_box, marketing_rules, recipient_suppressed, thread_participant,
last_outbound_asks_for, last_outbound_at, now}`. Output `QaResult{verdict approve|block|rewrite,
confidence, flags, rewrite_body?, rationale, run_id?, model?}`.

Pre-checks run **before** the model; any failure returns `block`, confidence 1.0, the flags
below, and no `agent_runs` row (`QaResult.precheck_only`):

| Flag | Check |
|---|---|
| `prohibited_claim:<inventory|under_contract|exclusive|guaranteed|properties_available>` | XC-07 denylist on subject and body |
| `opt_out_violation` | recipient is on `suppression` |
| `wrong_recipient` | `to` does not contain the thread participant (normalised email compare) |
| `repeated_request` | `asks_for` equals the last sent outbound's `asks_for` on the thread |
| `cadence_violation` | an outbound was sent on the thread less than 24 h ago |

Then the model reviews against `prompts.a5.MARKETING_RULES`, the thread (latest message
included), the facts and the current buy box. A `rewrite` without `rewrite_body` gets the flag
`rewrite_without_body` (the policy engine queues rewrites anyway). `contains_sensitive` adds the
flag `sensitive_content`.

Persistence: `QaReviewRepository.record(message_id=..., verdict, confidence, flags, rationale,
rewrite_body, model, agent_run_id)` writes one `qa_reviews` row and sets
`messages.qa_review_id` to it (INV-18).

## Policy step (XC-01, POL-03c)

`PolicyEngine.decide(PolicyAction.INVESTOR_REPLY, PolicyContext(qa_verdict, qa_confidence,
qa_flags, recipient_suppressed, sends_today, sensitive_content, now))`:

- L0 -> `queue_for_human`: Graph draft created with `createReply` on the inbound item (same
  conversation, INV-24), row `policy_decision=queue_for_human`, nothing sent.
- L1 -> `send` only with `approve`, confidence >= `policies.threshold` (default 0.85) and
  `sensitive_content=False`; daily cap honoured through `sends_today` (sent replies with
  `policy_action=investor_reply` today). Otherwise `queue_for_human`.
- QA `block` or suppressed recipient -> `blocked`: a local `messages` row
  (`external_message_id=reply:<inbound id>`, `meta.blocked=true`) records the draft and the QA
  review; nothing is created in the mailbox.
- A `rewrite` verdict sends/queues `rewrite_body`; the A4 original is kept in `meta.a4_body`.

Audit events: `message.classified`, `suppression.added`, `sequence.signalled`,
`buy_box.created`, `buy_box.updated`, `message.drafted`, `reply.decided`, `message.sent`,
`reply.approved`, `reply.rejected`. Every model call is an `agent_runs` row (XC-02).

## Signal contracts

### To the A3 `InvestorSequence` workflow (sent by `classify_inbound` / `notify_bounce`)

Target: `sequences.temporal_workflow_id` of the active or paused investor sequence on the
thread (`sequences.thread_id`), falling back to the investor's active sequence. Sent through
`get_temporal(settings).signal(workflow_id, name, payload)` with one dataclass argument. The
payload classes (and the signal names) are defined once in `wholesaling/signals.py` and imported
by both `activities/conversation.py` and `workflows/investor_sequence.py`, whose handlers are
`reply(payload: ReplySignal | None = None)` etc. (a bare signal still works; the payload is kept
in the workflow's `status.last_signal`). Both sides sharing the module is what closed pilot
GAP-01 (handlers that took no payload failed the workflow task on every signal).

| Signal name | Payload | When |
|---|---|---|
| `reply` | `ReplySignal(message_id: str, classification: str)` | every substantive class except `opt_out` (`interested`, `buy_box_supplied`, `question`, `not_now`, `not_interested`, `ambiguous`); never for `unrelated` or automatic replies |
| `opt_out` | `OptOutSignal(message_id: str)` | class `opt_out` (after the suppression row is written) |
| `bounce` | `BounceSignal(message_id: str, bounced_address: str | None)` | an NDR whose bounced address maps to a contact / sequence |

Signals are at-least-once (an activity retry after a crash may repeat one); the sequence
workflow must treat a duplicate as a no-op. Each signal is recorded once as a
`sequence.signalled` audit event keyed `a4:<message id>:signal:<name>` / `bounce:<message id>:signal`.

### From the review queue to `GraphInboundMessageWorkflow`

| Signal name | Payload | Effect |
|---|---|---|
| `human_decision` | `HumanDecision(approved: bool, edited_body: str | None = None, step: int | None = None, message_id: str | None = None, actor_id: str | None = None)` — the same field names as the sender's `db.repositories.review.HumanDecision`, so Temporal's converter maps one onto the other | `approved=True` sends the queued draft (with `edited_body` as the body when it differs from the Graph draft: a second reply draft keyed `reply:<id>:edited` is created and sent, the original row gets `meta.superseded_by`); `approved=False` discards it. `step` is for sequence drafts and ignored here; `actor_id` is the operator for the audit row. The first decision wins. |

The workflow id is `inbound_workflow_id(subscription_id, graph_id)` (`graph-inbound-<sha256[:32]>`).
The workflow passes its own id into `fetch_message` (`FetchMessageInput.workflow_id`, recorded
as the `inbound.workflow_started` audit event on the inbound row) and into `draft_and_qa_reply`
(`DraftReplyInput.workflow_id`); the activity prefers `activity.info().workflow_id` when it runs
inside Temporal and falls back to the argument otherwise (tests, the synthetic pilot). It stores
the id on the draft row as `meta["temporal_workflow_id"]`, the key
`ReviewRepository.resolve_workflow_id` reads for inbound-reply drafts; failing that the route
looks for the `inbound.workflow_started` event on the draft or on the inbound message it answers
(`meta["reply_to_message_id"]`). Pilot GAP-05. The review queue flips
the row (`policy_decision`, `body`, `meta["human_decision"]`) *before* signalling; the activity
therefore keeps its own idempotency marker in `meta["human_decision_applied"]`, compares an edit
against `meta["graph_draft_body"]` (what is really in the Graph draft) and never revives a row
with `meta["blocked"]`. The wait times out after `HUMAN_DECISION_TIMEOUT` (14 days): the workflow
ends with `human_decision="timeout"` and the draft stays queued; `apply_human_decision`
(`ApplyHumanDecisionInput(message_id, approved, edited_body, decided_by)`) can still be run
directly and is idempotent.

## Golden set (INV-22) — `tests/agents/golden/inbound_replies.json`

28 inbound examples covering interested, buy box supplied (including two buy boxes in one
email and a partial box), question, not now with / without a date, not interested, opt-out
("Unsubscribe", "stop emailing me", "remove me", sarcastic removal), sarcasm ("Oh sure, I LOVE
spam"), corrections ("I said 150k max, not 250k", asset-type correction), quoted old email text
(Outlook `-----Original Message-----`, Outlook header block, Gmail `wrote:`, `>` quotes),
out-of-office (header + subject, body only, model-flagged), ambiguous, unrelated.

Format per case:

```json
{
  "id": "correction_price_max",
  "subject": "RE: ...", "body": "...", "headers": {"auto-submitted": "..."},
  "history": [{"direction": "out", "body": "..."}, {"direction": "in", "body": "..."}],
  "model": {"classification": "...", "confidence": 0.9, "rationale": "...", "is_auto_reply": false, "reconnect_date": null},
  "expected": {"classification": "...", "is_auto_reply": false, "quoted_text_stripped": true,
               "reconnect_date": "2027-01-15", "stripped_text_contains": "...",
               "stripped_text_excludes": "...", "model_called": true},
  "extraction": {"current": {"default": {...criteria...}},
                 "model": {"buy_boxes": [...EXTRACT_SCHEMA items...], "rationale": ""},
                 "expected": {"buy_boxes": [{"buy_box_key": "default", "price_max": "150000",
                                             "unknowns": [...], "corrections": [{"field": "price_max", "old": "250000", "new": "150000"}]}]}}
}
```

In this build `tests/agents/test_a4.py` scripts `MockModelProvider` with each case's `model`
block, so the set verifies the pipeline contract and the deterministic processing (stripping,
auto-reply short-cut, unknowns, corrections, multi-box keys, Sonnet routing). BO-13's eval
harness reads the same file, drops the `model` blocks and compares live model output against
`expected`; `_meta.version` is bumped when cases change.

## Tests

- `tests/agents/test_a4.py` (72): quote stripper, auto-reply detection, schema invariants,
  routing override, golden classification and extraction, prompt content, draft post-processing.
- `tests/agents/test_a5.py` (19): each pre-check flag without a model call, model path
  (approve / rewrite / block / sensitive), `qa_reviews` persisted and linked.
- `tests/activities/test_conversation.py` (26): SQLite + mock Graph + mock model + mock Temporal:
  opt-out -> suppression + signal; interested -> `reply` signal; replay no-ops; auto-reply does
  not cancel; version 1 -> correction version 2 with history -> replay; two boxes; L0 queued in
  the same conversation; L1 sent; L1 price talk / low confidence / rewrite / daily cap queued;
  QA block and suppressed recipient never reach the mailbox; human approve / reject / edit;
  bounce signal; the review-queue contract (workflow id key, decision record, edited body).
- `tests/workflows/test_inbound_pipeline.py` (23): definition and signal contract, stage routing
  with Temporal stubbed, worker registration, and four `temporal`-marked end-to-end runs under
  the time-skipping server (L0 + approval, opt-out, L1, timeout).

## Decisions not covered by the spec

1. A4 `classify` runs on Sonnet (override); `extract` stays on Haiku. Flip
   `a4.MODEL_OVERRIDES` if extraction quality needs it.
2. Blocked drafts never reach Outlook: they are local `messages` rows so the review queue and
   digest can show them without a stray draft in the mailbox.
3. Buy-box upserts use `mode="merge"`: an email that states one field never nulls the others.
4. An edited human approval sends a *new* reply draft (Graph has no draft-update in our gateway);
   the original draft stays in Drafts with `meta.superseded_by`.
5. The human wait is bounded (14 days); the draft stays queued and actionable afterwards.
6. `BuyBoxRepository.get_by_source_message` gained an optional `buy_box_key` (and
   `list_by_source_message`) so two boxes from one email are each replay-safe; existing callers
   are unaffected.
7. Automatic replies are detected deterministically first (no model spend) and also honoured
   when the model flags them.
8. Signals to the sequence are recorded as audit events keyed per (message, signal) so a retried
   activity does not re-signal after a successful commit; duplicates before commit remain
   possible, hence the at-least-once note above.
