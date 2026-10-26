# A7 offer engine and S5 negotiation (build order BO-11)

REQUIREMENTS.md section 11 (A7-01..A7-19), SEL-09, SEL-11, POL-03g/h, WF-04, WF-05, ENG-Q10,
ENG-Q11. Code: `wholesaling/agents/a7/`, `wholesaling/agents/s5.py`,
`wholesaling/activities/offers.py`, `wholesaling/workflows/offer.py`,
`wholesaling/workflows/negotiation.py`, `wholesaling/db/repositories/offers.py`,
`wholesaling/integrations/data/property.py`. Every number comes from `config/a7.yaml` and
`config/sequences.yaml` (loaded by `config/tunables.py`); nothing numeric lives in code except
the per-item repair placeholders flagged below.

## Pipeline (ENG-Q11 order)

| Step | Row | What happens | Module | Verification after it |
|---|---|---|---|---|
| 1 | A7-02 | address -> parcel id; beds/baths/sqft/year/lot from county **and** provider | `verification.run_v1` | **V1** (A7-12) dual-source facts: any fact >10% apart pulls the third source; majority wins; no majority -> escalate |
| 2 | A7-03 | sold comps: same ZIP, +-20% sqft, +-15 yrs, <=180 d, <=0.75 mi; expand to tier 2 (1.0 mi / 270 d) then 3 (1.5 mi / 365 d) only while fewer than 3 qualify; tier recorded | `comps.pull_comps` | |
| 3 | A7-04 | ARV: price/sqft, trim 15% each end, weight recency x distance (linear 1.0 -> 0.5 at tier limits), weighted mean x subject sqft | `arv.compute_arv` | **V3** (A7-14) comp audit (pre-filters + Sonnet): remove flagged, recompute, <=2 iterations; then **V2** (A7-13) comp ARV vs AVM: >10% -> re-run comps at the next tier, <=3 passes, still >10% -> confidence -1 |
| 4 | A7-05 | repairs = rate[class] x sqft + disclosed line items; Houston foundation contingency | `repairs.estimate_repairs` | **V4** (A7-15) year built / last sale / listing remarks; evidence worse than stated -> class +1 with reason; foundation inferred -> contingency |
| 5 | A7-06 | MAO = ARV x 0.70 - repairs - 10,000 - closing (3% of the pre-closing basis) | `mao.compute_mao` | |
| 6 | A7-07 | offer = MAO x (1 - 0.07), rounded **down** to $500 | `mao.compute_mao` | **V5** (A7-16) `recompute.py` re-derives MAO and offer from the logged inputs with `fractions` + integer cents; mismatch -> offer BLOCKED + `alert.raised` audit (`a7.recompute_mismatch`) |
| 7 | A7-08 | 7 gates, each persisted on `offers.gates` | `gates.evaluate_gates` | |
| 8 | A7-09 | locked template; A5 QA | `templates.render_offer`, `activities.offers.qa_offer_draft` | |
| 9 | A7-10 | `PolicyEngine(OFFER_SEND)` -> send (Graph draft + send) / queue / blocked | `activities.offers.decide_and_send_offer` | **V6** (A7-17) immediately before send: re-pull comps at the tier used (audited-out comps stay out, new comps pass the pre-filters); ARV moved >5% -> recompute MAO, offer, V5, draft; gates re-run either way |
| 10 | A7-10 | hand to S5: start `NegotiationWorkflow` | `activities.offers.handoff_to_s5` | |

Confidence (A7-04): High >=5 comps, tier 1, spread <=12%; Medium >=3 comps, tier <=2, spread
<=20%; Low otherwise. Spread = (max - min) / weighted mean of the kept comps' price/sqft. Any
`escalate` outcome forces Low (gate 1 fails, human decides). No photos (SEL-06) drops one level.

Gates (A7-08): `arv_confidence` (>= Medium), `market_hard_cap` (<= market deal-band max, else
$350k, OPN-16), `min_pct_of_arv` (>= 35%), `asking_tolerance` (blank or within 25%),
`no_open_flags` (title_lien, hoa, multi_unit, non_residential, probate_without_authority,
co_owner_missing), `seller_not_suppressed`, `no_active_offer_on_parcel`.

## Worked example (fixture subject `1204 Oak Hollow Dr, Houston, TX 77008`)

Intake: Medium, disclosed `roof`, asking $100,000, 2 photos. Config: fee 10,000; market_pct
0.70; room 7%; closing 3%; round $500; cash close 21 d; rates 15/30/50/75.

1. County and provider both say 3/2, 1,600 sqft, 1985, 7,200 lot -> **V1 pass** (no third pull).
2. Seven tier-1 sales qualify (tier 1 recorded).
3. Price/sqft 140, 145, 148, 150, 155, 160, 162 -> trim 140 and 162 (floor(7 x 15%) = 1 each end).
   Weights = recency x distance:

   | comp | $/sqft | days | miles | recency | distance | weight |
   |---|---|---|---|---|---|---|
   | CL3 | 145 | 20 | 0.20 | 0.944444 | 0.866667 | 0.818518 |
   | CL5 | 148 | 60 | 0.40 | 0.833333 | 0.733333 | 0.611111 |
   | CL1 | 150 | 40 | 0.30 | 0.888889 | 0.800000 | 0.711111 |
   | CL2 | 155 | 90 | 0.50 | 0.750000 | 0.666667 | 0.500000 |
   | CL4 | 160 | 150 | 0.70 | 0.583333 | 0.533333 | 0.311111 |

   Weighted mean = 443.0738 / 2.951851 = **150.1004 $/sqft**; ARV = 150.1004 x 1,600 =
   **$240,161**; spread (160 - 145) / 150.1004 = 9.99% -> **High**.
   **V3 pass** (no flags). AVM $245,000 vs $240,161 = 2.0% gap -> **V2 pass**.
4. Repairs: Medium 30 x 1,600 = 48,000 + roof 12,000 = **$60,000**. Remarks "Well kept home" ->
   **V4 pass**, class stays Medium.
5. MAO: 240,161 x 0.70 = 168,112.70; - 60,000 - 10,000 = 98,112.70; closing 3% = 2,943.38 ->
   **MAO $95,169.32**.
6. Offer: 95,169.32 x 0.93 = 88,507.47 -> **$88,500**. **V5**: recompute gives MAO 9,516,932
   cents (= $95,169) and offer $88,500 -> match.
7. Gates: High >= Medium; 88,500 <= 350,000; 88,500 >= 84,056.35 (35%); |88,500 - 100,000| /
   100,000 = 11.5% <= 25%; no flags; not suppressed; no other offer on HC-0001 -> **all pass**.
8. Draft: "we can offer $88,500 in cash ... subject to inspection and signed agreement ... close
   with cash in 21 days"; A5 QA approves.
9. L0: queued for a human (`offers.status = pending_review`, draft row in the review queue). L1:
   `a7_gates_passed` and QA approve -> auto-send; V6 re-pull finds the same seven comps (0.0%
   drift) and the gates pass again. L1 run: `offer.sent`, status `sent`, `expires_at` = +14 d.
10. `NegotiationWorkflow` `negotiation:{offer_id}` started.

Other fixture subjects (all in `integrations/data/fixtures/properties.json`): `88 Pine Ridge Ln`
(2 tier-1 comps -> tier 2, Medium, offer $95,000); `4510 Maple Ct` (county 1,500 vs provider
1,750 sqft -> third source 1,520 agrees with county, V1 adjusted, offer $81,000); `61 Elm Loop`
(1,500 / 1,800 / 2,100: no majority -> V1 escalate -> Low -> human); `930 Cedar Bend` (non-arm's-
length, new build, renovated flip and lot-value outlier comps all removed by V3 iteration 1,
iteration 2 clean; remarks "as-is ... foundation ... investor special" bump Medium -> Heavy and add
the $12,000 Houston contingency; offer $40,500 < 35% of ARV -> gate fails -> human); `2201 Birch
St` (tier-1 ARV 10.7% off the AVM -> V2 re-runs at tier 2 -> 7.3% -> pass, Medium, offer
$149,500); `717 Willow Way` (clean; tests inject comps before send to exercise V6).

## Verification record (A7-18)

One `offer_verifications` row per (pass, iteration), unique on `(offer_id, pass_id, iteration)`;
a re-run overwrites. The offer's `calc_record` carries: `sources_used`, resolved `facts` with the
winning source per field, `comp_pull` (tiers tried, query per tier), `comps_kept`,
`comps_rejected` (comp + flags + reasons + stage), `arv_comps` (every weight), `arv_avm`,
`arv_confidence` + `confidence_notes`, `condition_class_intake` / `_final`, `repairs`, `mao`
(inputs + every intermediate), every gate, every verification record, escalations, draft and
status. Example V2 row (fixture `2201 Birch St`, iteration 1):

```json
{"pass": "V2", "iteration": 1, "result": "adjusted",
 "inputs": {"arv": {"arv": "294000", "tier": 1, "comp_count": 5, "...": "..."}, "avm": "262500",
            "tier": 1, "pass_cap": 3},
 "outputs": {"gap_pct": "0.1071", "action": "re-ran comps at tier 2", "arv_after": {"arv": "283572", "...": "..."}},
 "reason": "comp ARV >10% from AVM; re-running comps at tier 2"}
```

At L1 the operator reviews these records weekly (A7-18); the review queue shows the queued
offer draft with the number and the gate results.

## V3 comp audit

Deterministic pre-filters (`audit.prefilter_flags`) run first and are recorded with source
`prefilter`: new build when `year_built >= sale_year - 1` (or remarks say new construction);
lot-value outlier when price/sqft > 2.5x the set's median (or remarks say teardown / land
value); renovated flip when the prior sale was within 365 days at <= 70% of the resale (or recent
+ remarks say renovated); non-arm's-length when buyer and seller share a surname, remarks say
family / estate / quitclaim, or price/sqft < 60% of the median. The model (Sonnet via the per-agent
override `{"A7": {"comp_audit": ...}}`, `TaskKind.COMP_AUDIT`) then reviews the same comps with a
strict JSON schema and may add flags; it never removes a pre-filter flag. Flagged comps are
removed and the ARV recomputed; the loop stops at no new flags or 2 iterations.

## Offer template (A7-09)

`templates.OFFER_BODY` is locked: variables are seller first name, address, `$offer`, cash-close
days. `check_offer_body` enforces: the number is present; "subject to inspection and signed
agreement"; "close ... in N days" with N from config; no fee / spread disclosure; no buyer-lined-up
claim; no seller-agreement claim. It runs in the engine, again in `qa_offer_draft` (a tampered
body blocks without a model call) and on a human's edited body in `send_approved_offer`. Every
seller message carries the TX equitable-interest disclosure, identity, postal address and opt-out
(SEL-05). Reminders carry the number; re-engagements after expiry do not (a fresh A7 run sets a
new number).

## OfferWorkflow (WF-04)

Id `offer:{intake_id}`, input `OfferWorkflowInput(intake_id, seller_lead_id)` (the S4 builder
starts it by name). No sleeps. `compute_offer` -> `qa_offer_draft` -> `pre_send_check` (V6, re-QA
if the draft changed) -> `decide_and_send_offer`:

- `send` -> `handoff_to_s5`, done (`outcome=sent`).
- `queue_for_human` -> wait for `human_decision(HumanDecision)` (same dataclass as the investor
  sequence: `step`, `approved`, `edited_body`, `actor_id`, `message_id`; the draft `messages`
  row carries `meta.temporal_workflow_id` so the review queue resolves the workflow). Approved ->
  `pre_send_check` again (V6 is "immediately before send"); if the number moved the offer is
  re-queued for a fresh decision (max 3 rounds, then withdrawn); otherwise `send_approved_offer`
  (suppression and the template guard still apply) -> `handoff_to_s5`. Rejected ->
  `withdraw_offer`.
- `blocked` -> done. A V5 mismatch blocks inside `compute_offer` before QA.

## S5 NegotiationWorkflow (WF-05, SEL-09, POL-03h)

Id `negotiation:{offer_id}`, input `NegotiationInput(offer_id, seller_lead_id)`; the schedule
comes from `config/sequences.yaml` via `load_negotiation`. Durable timers: reminders at days
2/5/10 (locked template, A5 QA, `OFFER_SEND` policy; a queued reminder waits in the review queue
and the workflow moves on), `expire_offer` at day 14, re-engagement at days 60/90, then done.

Signals: `counter(CounterSignal(amount, message_id))` -> `evaluate_counter`: inside the room
(`offer <= amount <= MAO`; below our offer also counts) and `PolicyEngine(COUNTER_ACCEPTANCE)`
says auto (L1/L2) -> status `accepted`, `contract.requested`; at L0, above MAO, or on a lapsed
offer -> wait for `human_override`. `accept(AcceptSignal|None)` -> `accept_offer`,
`contract.requested`. `decline(DeclineSignal(reason)|None)` -> reason captured on the offer and in
`offer.declined`, a 90-day timer, then `requeue_seller` (lead back to `queued`).
`human_override(HumanOverride(action=accept|decline|withdraw, amount, reason, actor_id))`.

`contract.requested` (audit, payload `offer_id`, `seller_lead_id`, `price`) is where this side
stops: a human signs (POL-03i hard floor) and S6 starts from `contract.signed`.

## Decisions the spec did not cover

- **Closing costs (OPN-13)**: "3% of offer" is circular, so closing = 3% of the pre-closing MAO
  (ARV x pct - repairs - fee). Recorded in `mao.formula`.
- **Trim with few comps**: `floor(n x 15%)` off each end, reduced so at least 3 remain (no trim
  below 7 comps).
- **Confidence count** uses the comps that entered the calculation (after audit, before trim).
- **Line-item amounts** for roof / HVAC / foundation (A7-05 names them, gives no numbers): $12,000
  / $8,000 / $10,000 placeholders in `repairs.LINE_ITEM_DEFAULTS`, flagged UNVALIDATED like
  OPN-12; move to `config/a7.yaml` when validated. In Houston a disclosed or inferred foundation
  issue adds the fixed $12,000 contingency instead of the generic line.
- **No AVM**: the dual-method check cannot run, so V2 lowers confidence one level (recorded).
- **V1 with one source only**: the facts are used provisionally and the pass escalates.
- **V1 escalation** keeps the pipeline running on the county values so the human sees a complete
  record; the confidence is forced Low and gate 1 fails.
- **V3 removing comps below 3** does not re-expand tiers (literal "remove flagged, recompute");
  confidence falls to Low by the count rule.
- **V6 new comps** are passed through the deterministic pre-filters only (no model call before
  send); a rejected new comp is logged with `stage: V6`.
- **V6 after a human approval**: a changed number re-queues the offer rather than sending what the
  human did not see; after 3 such rounds the offer is withdrawn.
- **Counter after expiry / decline**: routed to a human (the number is stale); a counter below our
  own offer is treated as inside the room.
- **Reminders at L0** queue for a human and do not block the timeline.
- **Open flags**: unknown liens / HOA are data gaps on the record, not gates; a stated lien or HOA
  is. `co_owner_missing` fires when the question is unanswered or names unidentified co-owners.
- **Property data mode** rides on `DATA_PROVIDER_MODE` (no new setting); the mock's clock is the
  world clock for comp ages (`advance_clock`), `inject_comp` simulates a sale recorded between
  draft and send.
