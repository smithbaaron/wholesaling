# Seller funnel, part 1: S1 lists, S2 skip trace, S3 sequences, web form, S4 intake (BO-10)

REQUIREMENTS.md SEL-01..08, SEL-12, WF-03, WF-10, WF-21, POL-03d/e/f, POL-03l, POL-06, POL-09,
INT-04, INT-05, INT-26, CMP-01..03, CMP-05, XC-01, XC-03, XC-07, ENG-Q9. Everything runs in mock
mode: no vendor, no mailbox, no model, no Temporal server is assumed (RUL-01, RUL-02, RUL-06).

| Piece | Module | Role |
|---|---|---|
| Providers | `integrations/data/distress.py`, `skiptrace.py` | `DistressListProvider`, `SkipTraceProvider`, `DncListProvider` Protocols + fixture mocks (`fixtures/distress.json`, `skiptrace.json`, `dnc.json`); `DeedRecordsProvider.resolve_parcel(address)` added (mock resolves deed and distress addresses); `DataProviders` gained `distress` / `skiptrace` / `dnc` (append-only, default `None`); `get_data_providers` refuses `DISTRESS_PROVIDER_MODE` / `SKIPTRACE_PROVIDER_MODE` other than `mock` (INT-04 / INT-05 OPEN) |
| Blob storage | `integrations/blob/` | `BlobStorage` Protocol, `LocalDirStorage` (`BLOB_MODE=local`, a Railway Volume at `BLOB_LOCAL_DIR`), `MockBlobStorage`, `S3BlobStorage` documented stub (ENG-Q9; raises until a storage account exists) |
| S1 | `agents/s1.py` | `S1SellerListBuilder.run(session, providers, market)`: Active markets only, merge per parcel, derive `long_term_hold`, gates, dedupe, idempotent refresh |
| S2 | `agents/s2.py` | `S2SkipTrace.run(session, providers, seller_lead)`: vendor lookup -> normalise, dedupe, suppression, DNC scrub (recorded), primary email |
| Templates | `agents/templates/seller_outreach.py` | `TX_EQUITABLE_INTEREST_DISCLOSURE`, initial + 3 follow-ups (`SellerTemplate`, `legal_review="pending"`), `render_seller_message`, `missing_compliance_blocks`, `ensure_compliance_block` |
| S3 | `agents/s3.py` | `S3SellerOutreach.draft/sequence`, `SellerSequencePlan` (3/7/14 from `config/sequences.yaml`), `SmsGateway` Protocol + `DisabledSmsGateway` (always raises) |
| S4 | `agents/s4.py`, `agents/prompts/s4.py` | `S4IntakeAgent.classify` (Haiku), `.extract` (Haiku), `.parse_form` (no model), `.draft_reply` (Sonnet); `choose_asks`, `assemble_reply_body`, `photo_upload_link` |
| Repository | `db/repositories/sellers.py` | `SellerRepository` (dedupe keys, web-form merge, intake merge with `*_unknown` flags, threads, form messages); `missing_required`, `unasked_optional` |
| Activities | `activities/seller.py` | `seller_list_refresh`, `seller_prepare_step`, `seller_decide_and_send`, `seller_send_approved`, `seller_classify_inbound`, `seller_intake`, `seller_apply_human_decision`, `seller_notify_bounce`; `SellerContext`; the names the A7 builder codes against |
| Workflows | `workflows/seller_sequence.py`, `workflows/seller_inbound.py` | `SellerSequenceWorkflow`, `SellerListRefreshWorkflow` (weekly schedule `seller-list-refresh`), `SellerInboundWorkflow` |
| Web form | `api/routes/sell.py`, `api/templates/sell/` | `GET|POST /sell`, public, per-IP rate limit, photo upload, SEL-12 idempotency |
| Settings | `DISTRESS_PROVIDER_MODE`, `SKIPTRACE_PROVIDER_MODE`, `PUBLIC_BASE_URL`, `SELL_FORM_RATE_LIMIT_PER_MINUTE`, `SELL_FORM_MAX_PHOTOS`, `SELL_FORM_MAX_PHOTO_BYTES` | plus the existing `BLOB_MODE` / `BLOB_LOCAL_DIR`, `SENDER_*`, `OPT_OUT_TEXT`, `SEND_WINDOW_*` |

## The loop

```
weekly: SellerListRefreshWorkflow -> seller_list_refresh (every Active market)
  S1  distress lists -> merge per parcel -> long_term_hold -> gates -> dedupe -> seller_leads (queued)
  S2  for each queued lead without a trace: skip trace -> contact_channels (+ DNC scrub) -> primary email
      no email  => audit seller_lead.unreachable (reported, never mailed)
  start_seller_sequence: sequences row (kind=seller) + audit sequence.started +
                         start_workflow("SellerSequenceWorkflow", id=seller-sequence:{sequence_id})
                         lead.status = in_sequence

SellerSequenceWorkflow (steps 0..3, sleeps 3 / 7 / 14 days)
  check_cancel_conditions -> send_window -> seller_prepare_step (S3 template + SEL-05 block)
  -> seller_decide_and_send (A5 QA on every draft, PolicyEngine(seller_outreach_email))
       send | queue_for_human (wait human_decision -> seller_send_approved) | blocked (cancel)
  -> update_sequence -> durable sleep; signals reply / opt_out / bounce / intake_complete cancel

seller reply (email) or web form
  -> SellerInboundWorkflow: fetch_message | form row -> seller_classify_inbound (S4 classify;
     opt_out => suppression + signal; reply => signal) -> seller_intake
       S4 extract (or parse_form) -> merge intake -> resolve_parcel -> missing_required
       complete  => intake.completed, start_workflow("OfferWorkflow", id=offer:{intake_id}),
                    signal intake_complete to the sequence, no S4 reply (A7 answers)
       missing   => S4 reply asking <= 2 fields (+ photo link) -> A5 QA ->
                    PolicyEngine(seller_intake_reply) -> send | queue_for_human | blocked
  -> queued? wait human_decision (14 d) -> seller_apply_human_decision
```

## S1 (SEL-01, SEL-02, WF-10)

- **Market gate**: `market.tier` must be `Active`; otherwise `S1Result.refused_reason =
  "market_not_active"` and nothing is pulled. The weekly schedule runs over Active markets only;
  a direct call on a Watch market returns the refusal instead of raising.
- **Merge**: vendors return one row per list; rows are grouped by `parcel_id` and their signals
  unioned. `long_term_hold` is derived from `last_sale_date` (>= 15 years before `as_of`) in
  addition to whatever the vendor flags.
- **Gates**, in order: residential 1-4 unit (`sfr`, `duplex`, `triplex`, `fourplex`, 1-4 units);
  a value to band (assessed first, else estimated; none => `no_value`); inside the market deal
  band (`markets.deal_band_*`, else `config/deal_band.yaml`); >= 2 stacked signals.
- **Score**: stacked points per signal (`SIGNAL_WEIGHTS`: pre-foreclosure 30, tax 25, probate 25,
  absentee 15, vacant 15, code violation 15, long-term hold 10, expired listing 10). Documented
  defaults, not V2 numbers; `queued_lead_ids` is ordered by score.
- **Suppression / dedupe**: owner on `reference_lists.suppressed` => `suppressed`; an existing
  lead for the parcel id or the normalised address (`normalize_address`) is refreshed (signals,
  score, values, source refs), never duplicated; a lead whose status is `suppressed` or `dead`
  is never re-queued (`lead_suppressed_or_dead`).
- Rerunning on the same lists: `created=0`, `refreshed=N`.

Houston fixture (`fixtures/distress.json`, 11 rows / 10 parcels): Ramirez (two lists merged +
long hold, score 80), Boyd estate (probate + code violation + vacant + long hold), Nguyen (one
vendor signal + derived long hold), Pike (duplex, expired + vacant), Okafor (absentee + vacant)
are queued; Lowell ($520k) is outside the band, a 12-unit building and a lot are not residential,
one parcel has no value, Suppressed Holdings LLC is on the suppression list. Columbus, OH has one
row for the market filter.

## S2 (SEL-03, CMP-02)

`SkipTraceProvider.trace(owner, property address, mailing address)` -> candidates with
provenance and confidence. S2 normalises (`normalize_email` / `normalize_phone`), drops junk
(`n/a`, unparseable), folds duplicates per `(channel, normalised value)` keeping the highest
confidence, drops nothing but marks emails on `suppression` as `suppressed` (never primary), and
scrubs every phone against `DncListProvider`. Written to `seller_leads.contact_channels` (one
dict per contact: `channel`, `value`, `normalized_value`, `source`, `provenance`, `confidence`,
`use_limitations`, `is_primary`, `dnc_listed`, `suppressed`) and
`seller_leads.source_refs["skip_trace"]` (`vendor`, `vendor_ref`, `dnc_checked`, `dnc_listed`,
`preferred_channel="email"`, `sms_allowed=False`). Phones carry the limitation "capture only: no
calls or SMS in this build". The highest-confidence non-suppressed email is primary; none =>
`seller_lead.unreachable`.

## S3 and the SEL-05 compliance block

Every seller-facing message = rendered copy + `TX_EQUITABLE_INTEREST_DISCLOSURE` + the CAN-SPAM
footer (`investor_outreach.footer`: identity, postal address, opt-out). `missing_compliance_blocks`
checks the four pieces; `seller_decide_and_send` **refuses** (raises) a sequence draft lacking any
of them, `seller_send_approved` and `seller_apply_human_decision` re-append the block to a human
edit, `assemble_reply_body` appends it to every S4 draft, and a QA rewrite is passed through
`ensure_compliance_block`. The disclosure wording is `legal_review="pending"` (CMP-04, PRE-16):
plain language that we are a wholesaler who may assign the equitable interest in a purchase
contract for a fee, not a licensed broker, not representing the seller. The structure is fixed;
counsel may change the words. All four copy templates are `pending_approval` (PRE-16).

Policy: every step is `seller_outreach_email` (POL-03d; L0 queue, L1 QA-approved under the
`policies.daily_cap`, L2 auto). `seller_outreach_sms` is OFF; `DisabledSmsGateway.send` raises
and no code path calls it (POL-03e, RUL-06). Subject variants: two, assigned by `seller_lead_id`.
Steps 1-3 reply in the step-0 conversation (`_ensure_graph_draft` reused).

## S4 (SEL-06, SEL-07, SEL-08)

| Task | Model | Output |
|---|---|---|
| `classify(thread)` | Haiku | `S4Classification{classification: reply / not_interested / opt_out / unrelated, confidence, is_auto_reply, asks_offer}`; auto-replies detected deterministically first (A4's detector), no model call |
| `extract(thread, known)` | Haiku | `S4Extraction{fields: IntakeFields}`; every field nullable with `null` first, so nothing is invented |
| `parse_form(payload)` | none | the web form's fields as `IntakeFields` |
| `draft_reply(thread, known, asks, photo_link, asks_offer)` | Sonnet | `S4ReplyDraft{subject, body, asks_for (clamped to `asks`, max 2), contains_sensitive}` |

Stored classification: `messages.classification` gets the shared enum (`reply` -> `interested`,
`not_interested`, `opt_out`, `unrelated`; an automatic reply is stored as `unrelated` with
`meta.a4.is_auto_reply=True` so `thread_cancel_reason` ignores it, INV-22); the S4 record is
`messages.meta["s4"]`.

Required fields (`missing_required`): `address` (parcel-verified), `motivation`, `condition`
(class; `disclosed_issues` ride along), `timeline` (`unknown` counts as missing). Asked but
optional: `co_owners`, `occupancy`, `liens`, `hoa` - stated => `*_unknown=False`; "I don't know"
=> stays unknown, recorded in `intakes.property_facts["seller_unsure"]`, not re-asked. `choose_asks`
takes up to two: missing required first, then one optional field not yet asked
(`property_facts["asked_optional"]`). Photos: when `intakes.photos` is empty the reply carries
`PUBLIC_BASE_URL/sell?lead=<seller_lead_id>` and `photo_link_sent_at` is stamped; the offer
proceeds without photos (A7 drops confidence one level per SEL-06).

Parcel verification: `providers.deeds.resolve_parcel(address)` on the extracted / form address;
success stamps `parcel_id`, `parcel_verified`, the county's address fields and `address_key` and
audits `seller_lead.parcel_verified`. An S1 lead (already verified from the county list) stays
verified when the seller restates the address loosely; a web-form lead with an unknown address is
asked to confirm it.

Completion: when nothing required is missing, `intakes.intake_complete=True`, `completed_at`,
audit `intake.completed`, then `offer.requested` = `start_workflow("OfferWorkflow",
[OfferWorkflowInput(intake_id, seller_lead_id)], workflow_id="offer:{intake_id}")`, audited once
(`intake:{id}:offer_requested`), then the seller sequence gets `intake_complete`. No S4 reply is
sent on completion: A7's offer is the next message.

Sensitivity (POL-06, POL-09): `contains_sensitive` = model flag OR a seller-side scan (dollar
figures, `150k`, contract / earnest / attorney / legal / commission / guarantee / worth /
valuation) OR the seller asked for a number (`asks_offer`). Unlike A4, the bare word "offer" is
allowed. A sensitive draft is **always** queued for a human, even when `seller_intake_reply` is
at L1/L2 (`policy_reason` says "hard floor ... POL-09").

## Web form (SEL-12, INT-26)

`GET /sell` renders the form (address, name, email, phone, motivation, condition class, known
problems, timeline, asking price, up to 10 photos of 10 MB, consent-to-email checkbox, the
disclosure and sender identity on the page) with a fresh UUID in the hidden `submission_id`;
`?lead=<seller_lead_id>` (S4's upload link) is carried in a hidden field. `POST /sell` validates,
stores photos as `sell/<submission id>/<n>.<ext>` through `BlobStorage`, creates the lead
(`source=web_form`, `status=intake`) or merges into the lead behind the token or the same
normalised address, writes the `web_form` message row (`form:<submission id>`), adds the photos to
the intake, audits `web_form.submitted`, commits, and starts `SellerInboundWorkflow` with id
`seller-inbound:form:<submission id>`. A re-POST with the same `submission_id` renders the
thank-you page again and changes nothing. Rate limit: `SELL_FORM_RATE_LIMIT_PER_MINUTE` per client
IP (`X-Forwarded-For` first), in process, 429 when exceeded. No IP or user agent is stored (CMP-05).

## Signal and id contracts

| What | Value |
|---|---|
| Seller sequence workflow | type `SellerSequenceWorkflow`, id `seller-sequence:{sequence_id}`, input `SellerSequenceInput(sequence_id, seller_lead_id, contact_email, market, intervals_days=[3,7,14], honor_send_window=True)` |
| Signals to it | `human_decision(HumanDecision)` (the investor dataclass, review-queue compatible), `reply(ReplySignal)`, `opt_out(OptOutSignal)`, `bounce(BounceSignal)`, `intake_complete(IntakeCompleteSignal(intake_id, seller_lead_id))`, `pause()`, `resume()` |
| Seller inbound workflow | type `SellerInboundWorkflow`, id `seller-inbound:email:{graph_id}` or `seller-inbound:form:{submission_id}`, input `SellerInboundInput(source, graph_id, subscription_id, change_type, message_id, submission_id)`; signal `human_decision(HumanDecision)` (the `graph_maintenance` dataclass); the queued draft stores `meta.temporal_workflow_id` for `ReviewRepository.resolve_workflow_id` |
| Offer hand-off (A7 builder) | `start_workflow("OfferWorkflow", [OfferWorkflowInput(intake_id: str, seller_lead_id: str)], workflow_id=f"offer:{intake_id}")`; the dataclass lives in `activities/seller.py` and is matched by field name, their module is never imported |
| Reply draft key | `messages.external_message_id = seller-reply:{inbound message id}` (`:edited` for a human edit) |
| Audit events | `sequence.started`, `seller_lead.unreachable`, `outreach.step_prepared`, `outreach.qa_reviewed`, `outreach.policy_decided`, `outreach.human_approved`, `message.classified`, `suppression.added`, `sequence.signalled`, `seller_lead.parcel_verified`, `intake.completed`, `offer.requested`, `intake.reply_decided`, `reply.approved`, `reply.rejected`, `web_form.submitted` |

## Tests

- `tests/integrations/data/test_distress_skiptrace.py` (11), `tests/integrations/blob/` (12).
- `tests/agents/test_s1.py` (34): rules, gates, Houston scenarios, idempotent refresh,
  address dedupe, final statuses, market band. `test_s2.py` (7): dedupe, provenance, DNC
  recorded, suppression, unreachable. `test_s3.py` (18): disclosure + four blocks on every
  template, denylists, drafts, plan, SMS off. `test_s4.py` (48): schemas, form parsing, golden
  set (`tests/agents/golden/seller_intake.json`, 18 cases), draft post-processing, routing.
- `tests/activities/test_seller.py` (36): refresh + S2 + starts, idempotency, start failure,
  sequence steps at L0/L1 (cap, block, suppression, disclosure refusal, edit re-append), classify
  (opt-out, reply, not interested, auto-reply, stranger, email match), intake (partial with photo
  link, complete -> OfferWorkflow + signal, two-message completion, price question queued at L1,
  L1 auto-send, QA block / rewrite, address fix, unreachable), web form source, human decisions,
  bounce, agent-run logging.
- `tests/api/test_sell_form.py` (22): page, no auth, happy path, idempotent re-POST, token /
  address merge, validation, photo limits, rate limit.
- `tests/workflows/test_seller_sequence_definition.py` (9), `test_seller_sequence_sim.py` (14),
  `test_seller_inbound_definition.py` (18); `temporal`-marked e2e:
  `test_seller_sequence_workflow.py` (3), `test_seller_inbound_workflow.py` (3).

## Decisions not covered by the spec

1. Signal weights, the 15-year hold derived from `last_sale_date`, assessed-before-estimated
   banding, and "no value => rejected" are S1 defaults to retune.
2. Address suppression: `suppression` is channel-keyed (email / sms / web form), so S1 suppresses
   by owner entity key (reference list) and by the lead's own final status
   (`suppressed` / `dead`), plus parcel / address dedupe. No schema change.
3. `seller_leads` has no `meta` column: skip-trace provenance and the DNC scrub live in
   `source_refs["skip_trace"]`; the lead's traced contacts in `contact_channels`.
4. Seller classification stores the shared `MessageClassification` enum (`reply` -> `interested`)
   so `thread_cancel_reason` and the review queue work unchanged; the S4 record is
   `meta["s4"]`. `not_interested` sets the lead `dead`; `opt_out` sets it `suppressed`.
5. No S4 reply is sent when the intake completes; A7 answers with the offer.
6. A sensitive intake reply (price figure, contract / legal terms, "what's your offer") is queued
   for a human at every level - the POL-06 / POL-09 hard floor applied in code because the
   `seller_intake_reply` row is plain AUTO at L1.
7. The web form upload link carries the seller lead id as the token (no signing); the token only
   lets photos be attached to that lead and is only ever sent to the seller's own address.
8. Web form submissions are merged into an existing lead for the same normalised address (the
   same property S1 found) rather than creating a second lead.
9. `check_cancel_conditions`, `send_window` and `update_sequence` are reused as is; the
   `sequence.cancelled` / `sequence.completed` audit rows they write carry
   `actor_id="investor_sequence"` for seller sequences too (the `sequences.kind` column
   disambiguates).
10. The Graph webhook still starts `GraphInboundMessageWorkflow` for every notification;
    `SellerInboundWorkflow` is self-sufficient (unknown sender => `not_seller`) so routing by
    thread is a one-line follow-up for the webhook owner. The web form and the synthetic seller
    pilot are its callers today.
11. `DataProviders` gained three optional fields instead of a second bundle so S1/S2 take the
    same `providers` argument as A1/A2; `healthcheck()` still lists the original three.
12. `BLOB_MODE=s3` is a documented stub that raises at construction (ENG-Q9); the local store is
    the pilot's Railway Volume.
