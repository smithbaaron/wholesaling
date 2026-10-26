# Disposition: contracts, S6 buyer blast, deadlines, closing (BO-12)

REQUIREMENTS.md SEL-10, SEL-11, BB-09, BB-11, WF-06, WF-23, POL-03i/j/k, POL-06, XC-05, CMP-01,
CMP-03, PRE-13, PRE-14. Mock mode only in this build (RUL-06): DocuSign is mocked
(docs/integrations/docusign.md), nothing signs, nothing moves money.

## The deal path (SEL-11)

```
A7/S5: offers.status = accepted, audit contract.requested
   │
   ▼  /deals "awaiting contract"
human: Generate PSA envelope ──► contracts row (psa, requested → sent_for_signature)
                                 DocuSign envelope created as a DRAFT (status "created")
human: sends + signs in DocuSign, reads the executed contract
human: Mark contract signed ───► contracts.status = signed, signed_by, signed_at, envelope id
                                 audit contract.signed (actor = human)
                                 DispositionWorkflow started, id disposition:{contract_id}
   │
   ▼  DispositionWorkflow (WF-06)
S6 blast to buyers (A5 QA → PolicyEngine BUYER_DISPOSITION_BLAST → send | queue | blocked)
deadline alerts T-3d / T-1d / T-0 for option period, inspection, closing  (audit alert.raised)
signals from /deals: buyer_response, buyer_selected, deposit_received, deadlines_updated,
                     human_decision (review queue), deal_dead, deal_closed
   │
   ├─ human: Mark closed (realised assignment fee)  → status closed (BB-11)      → workflow ends
   └─ human: Mark deal dead                          → status cancelled; timers off;
                                                      release-earnest-money reminder (human)
```

Every box labelled *human* is a `POST` on `/deals`, operator-authenticated, written through
`ContractRepository` with `actor = human`, audited under the operator's name, and only then
signalled to the workflow. The repository raises `HumanActionRequired` for any other actor on
`mark_sent_for_signature`, `mark_signed`, `set_earnest_money`, `set_deadlines`, `select_buyer`,
`record_deposit`, `update_checklist`, `mark_closed`, `mark_dead`, `create_assignment`
(`tests/db/test_contracts.py::test_automated_actor_cannot_sign_or_touch_money`). The one write an
automated path may make is `record_buyer_response` (interested / pass + proof of funds + deposit
terms), and it never selects the buyer.

## `contracts` table (migration 0003)

One row per agreement. The **PSA** row is the deal record; the **Assignment Agreement** is a
second row (`type = assignment`, `parent_contract_id` → the PSA). `UniqueConstraint(offer_id,
type)`.

| Column group | Columns |
|---|---|
| identity | `offer_id`, `seller_lead_id`, `parent_contract_id`, `type` (`psa`/`assignment`), `status` |
| property | `property_address_line1/city/state/postal_code`, `purchase_price` |
| e-signature | `docusign_envelope_id`, `envelope_status`, `sent_for_signature_at`, `signed_at`, `signed_by` (human) |
| earnest money (POL-03j) | `earnest_money_amount`, `earnest_money_status` `none → requested → deposited → released` (and `requested → none`) |
| deadlines | `option_period_end`, `inspection_deadline`, `closing_date` |
| buyer | `buyer_investor_id`, `assignment_fee`, `deposit_amount`, `deposit_received_at`, `buyer_responses` JSON |
| closing / death | `realized_assignment_fee`, `closed_at`, `deal_dead_reason` |
| coordination | `title_company`, `checklist` JSON (title opened, earnest delivered, assignment delivered, buyer deposit at title, closing scheduled), `meta` (workflow state mirror) |

Status values: `requested`, `sent_for_signature`, `signed`, `closed`, `cancelled`. **`closed` was
added to the spec's list** so the realised assignment fee (BB-11) has a terminal state distinct
from "dead".

Audit event types (`entity_type = contract`, `correlation_id = disposition:{psa_id}`):
`contract.created`, `contract.sent_for_signature`, `contract.signed`, `contract.earnest_money`,
`contract.deadlines_set`, `contract.buyer_response`, `contract.buyer_selected`,
`contract.deposit_received`, `contract.checklist_updated`, `contract.closed`, `contract.dead`,
`contract.dead_followup`, `alert.raised` (kinds `deal_deadline`, `earnest_money_release`),
`disposition.workflow_started`. `contract.requested` is written by the A7/S5 builder on the offer.

## S6 — buyer selection and copy (`agents/s6.py`)

Runs only for a **signed** contract (the template refuses otherwise; `run_blast` drafts nothing
for any other status — BB-09 "only approved deals may be described to buyers").

**Who** (`select_buyers`), tier order, then within a tier *prior closed deals with us first*
(`contracts.status = closed` where they were the buyer), then A1 priority `score` desc, then
`purchase_count_24m` desc, then name:

1. Tier 1 — investors behind **approved** D2 matches (`matches.review_status = approved`) whose
   property candidate is this property (same seller lead, address key or parcel). Pending or
   rejected matches do not qualify.
2. Tier 2 (fallback / widening) — investors in the deal's market with a *current, active* buy box
   whose price band contains the assignment price (contract price + our fee) and whose geography
   lists the property ZIP, or lists no ZIPs.

Only investors with an email contact are returned; excluded investors (`excluded_reason`) never
are; suppression is honoured at send time. "Priority score" = `investors.score` (A1); the spec
left the exact ordering open, this is what exists.

**What** (`templates/disposition.py`, one template, `pending_approval` like the follow-ups):

- says the property is **under contract** and that we hold an *equitable interest under an
  assignable purchase agreement and are not the owner* (TX disclosure, CMP-03);
- says "The seller has agreed only to the terms of that agreement" and "Nothing is held for
  anyone" — and the guard (`DISPOSITION_PROHIBITED`) refuses any value or rendered text with a
  seller-agreement claim beyond that (`seller agreed to`, `seller accepted`, `seller will accept`,
  …), `guaranteed`, `exclusive`, `held/reserved for you`, `we own`, urgency phrases;
- evidence summary from the offer record: ARV with confidence and comp tier, repairs ("desk
  estimate, not an inspection"), open flags to verify, source line (BB-09 evidence + uncertainty);
- asks for proof of funds and earliest closing date; CAN-SPAM footer (identity, postal address,
  opt-out) from `investor_outreach.footer` (CMP-01).

The `investor_outreach.render` guard is not reused on purpose: it forbids "under contract"
because an outreach draft has no deal behind it.

**QA** — `S6QaAgent` is A5 with one exemption: the deterministic `prohibited_claim:under_contract`
pre-check is lifted **only when the contract row is signed**. A5's own marketing rule reads "unless
the facts given say so", and the signed row (human-recorded) is that fact; the QA prompt receives
`contract_status: signed`. Every other pre-check (suppression, wrong recipient, guarantees,
exclusivity, inventory) and the model review stay in force.
`tests/agents/test_s6.py::test_s6_qa_lifts_only_the_under_contract_precheck_when_signed`.

**Policy** — `BUYER_DISPOSITION_BLAST`: L0 draft (queued), L1 auto for approved deals
(`deal_approved = contract.status == signed` and QA `approve`), L2 auto. Queued drafts carry
`meta.temporal_workflow_id = disposition:{contract_id}` so the existing review queue
(`/review`) approves them through the same `human_decision` signal; the workflow then runs
`send_approved_blast` (footer kept, suppression re-checked). Draft rows are keyed
`disp:{contract_id}:{investor_id}`: a retried blast never re-sends.

## Buyer responses, selection, assignment, deposit

- `record_buyer_response(contract_id, investor_id, response, proof_of_funds, deposit_terms)` is an
  activity callable by anything (the `/deals` form today; the inbound pipeline later when it
  classifies a buyer's reply). It writes `buyer_responses[investor_id]` and signals the workflow.
- **Select buyer** (human) requires a signed PSA; records the chosen `assignment_fee` /
  `deposit_amount` (a dollar commitment outside A7's formula → human, POL-06).
- **Generate assignment** (human) creates the `assignment` row and its DocuSign *draft* envelope
  (Assignee signs first, then our entity); **Mark assignment signed** records it.
- **Record deposit** (human) needs a selected buyer and a positive amount.
- **Earnest money** (human only, POL-03j): `requested` → `deposited` → `released`; the deal-dead
  path never releases it, it raises a reminder.
- Title/escrow coordination = the checklist fields + the deadline alerts; the title company per
  market is PRE-14.

## Deadline alerts (WF-06, WF-23)

Decision: **no separate `DeadlineAlertWorkflow`.** The timers belong to the deal: they must stop
the instant it dies or closes and move whenever a human corrects a date; one workflow per contract
owning them cannot drift from the deal's state. `workflows/deadlines.py` is the pure schedule
arithmetic (three deadlines × T-3d / T-1d / T-0, `DEFAULT_ALERT_OFFSETS_DAYS`, overridable per
start), and `DispositionWorkflow` arms `wait_condition(timeout=next_due)`.

Each alert = `raise_deadline_alert` → `alert.raised` audit on the contract (idempotency key
`disp:{contract_id}:alert:{deadline}:{label}`), a WARNING log line, and a Slack post when
`SLACK_WEBHOOK_URL` is set (best effort; the audit row is the record). An alert already in the
audit log is never raised twice (the snapshot carries `raised_alerts`, so a worker restart or a
`deadlines_updated` re-read is safe, WF-22); an alert whose time passed while the deal was being
set up fires once immediately. There is no email sink in the digest module, so none here either.

## `DispositionWorkflow` (WF-06)

Started only by the human mark-signed route (`disposition:{contract_id}`). Loop: `load_contract`
→ refuse unless `signed` → `run_blast` → wait on timers/signals → `raise_deadline_alert` as they
come due → `deal_dead` (timers cancelled, `deal_dead_followup`, release reminder) or `deal_closed`
(state recorded, return). `record_workflow_state` mirrors `state`, `next_alert_key/at`, blast
counts onto `contracts.meta.workflow` so `/deals` can show "next alert". The workflow never changes
contract status: the human route did that before signalling. Query `status`.

Signal contract (`wholesaling/signals.py`): `buyer_response(BuyerResponseSignal)`,
`buyer_selected(BuyerSelectedSignal)`, `deposit_received(DepositReceivedSignal)`,
`deadlines_updated()`, `human_decision(BlastHumanDecision)` (same shape as the sequence
workflow's `HumanDecision`), `deal_dead(DealDeadSignal)`, `deal_closed(DealClosedSignal)`.

## `/deals` operator page

`GET /deals` (HTML), `GET /api/deals` (JSON). Actions, each a human click with an audit row:

| Route | Action |
|---|---|
| `POST /deals/offers/{offer_id}/psa` | PSA row + DocuSign draft envelope (`seller_name`, `seller_email` default from the lead; `title_company`, `earnest_money_amount`) |
| `POST /deals/{id}/signed` | **mark contract signed** (`signed_by` defaults to the operator, `signed_at`, `envelope_id`, deadlines, `title_company`); starts the workflow; 502 and nothing recorded if the start fails |
| `POST /deals/{id}/earnest` | `status` + `amount` |
| `POST /deals/{id}/deadlines` | option / inspection / closing / title company → `deadlines_updated` |
| `POST /deals/{id}/buyer-response` | `investor_id`, `response`, `proof_of_funds`, `deposit_terms` |
| `POST /deals/{id}/select-buyer` | `investor_id`, `assignment_fee`, `deposit_amount` |
| `POST /deals/{id}/assignment` / `…/assignment/signed` | assignment row + draft envelope; mark it signed |
| `POST /deals/{id}/deposit` | `amount`, `received_at` |
| `POST /deals/{id}/checklist` | `item`, `value` |
| `POST /deals/{id}/closed` | `realized_assignment_fee`, `closed_at` (BB-11) |
| `POST /deals/{id}/dead` | `reason`, optional `void_envelope=true` |

HTMX requests get a partial, plain form posts redirect to `/deals`, API clients get JSON. A
signal failure after the write is reported in `detail` (the human's record stands; the workflow is
idempotent on row state).

## KPIs (BB-11, append-only in `reporting/kpis.py`)

`contracts_signed`, `contracts_closed` (count + realised fees), `contracts_dead`,
`contract_to_close_totals` (all-time signed / closed), `contract_kpis` → `ContractKpis` with
`contract_to_close_rate` and `average_realized_fee_usd`. The pre-existing `contracts()` proxy
(accepted offers) is untouched; the digest can switch when the seller side goes live.

## Decisions the spec did not cover

1. `closed` added to the contract status list; `realized_assignment_fee`, `closed_at`,
   `parent_contract_id`, `envelope_status`, `sent_for_signature_at`, `buyer_responses`,
   `checklist`, `meta` added to the table.
2. Assignment Agreement = second `contracts` row linked by `parent_contract_id` (one `type` column
   as the spec asked, one deal record on the PSA row).
3. Deadline alerts folded into `DispositionWorkflow` (above); `workflows/deadlines.py` holds the
   pure arithmetic.
4. A5 `under_contract` pre-check lifted only for a signed contract, in a subclass, never in A5.
5. Envelopes are created as drafts (`status: created`), not `sent`; the human sends from DocuSign.
6. No Connect webhook route yet: a webhook may never mark a contract signed (SEL-11); the payload
   model and HMAC check exist for a status-mirror route later.
7. Slack via the existing `SLACK_WEBHOOK_URL` directly (the digest's sink needs a `KpiReport`);
   no email sink exists in the project.
8. Tier ordering uses what exists: closed deals in `contracts`, then `investors.score`, then
   `purchase_count_24m`.
9. `tests/db/test_migrations.py` now asserts `len(tables) == len(TABLE_NAMES)` instead of `20` so
   builders adding tables do not collide on a literal.
