# Milestone report — Build order steps 10 to 12 (seller side in mock mode)

Date: 2026-10-26. Reported per REQUIREMENTS.md RUL-08. CI green on `main`.

## What was implemented

| Step | Delivered |
|---|---|
| BO-10 Seller funnel | S1 distress-list builder (Active markets only, stacked signals ≥2, deal band, dedupe, weekly schedule); S2 skip trace with provenance and a DNC scrub recorded on every lead (SMS still never sends); S3 seller sequence 3/7/14 days in one thread, every message carrying the TX equitable-interest disclosure, sender identity, postal address and opt-out (`legal_review: pending` on the disclosure text — PRE-16); public `/sell` web form with photo upload to blob storage, idempotent submissions, IP rate limit; S4 conversational intake (asks at most two missing fields per reply, sends the photo-upload link, never invents) → `intake_complete` → `OfferWorkflow`; seller inbound pipeline; Graph webhook now hands seller-lead threads to the seller pipeline; 18-case intake golden set |
| BO-11 Offer engine + deals | A7: 10-step pipeline per A7-02..10 with V1–V6 verification loops in the ENG-Q11 order, every pass persisted in `offer_verifications`, an independent re-implementation for V5 that must match to the dollar, the full gate list persisted per gate, V6 re-pull before send, locked offer template, A5 QA, policy (L0 draft / L1 auto when every gate passes), `OfferWorkflow`; S5 `NegotiationWorkflow` (counters within room auto at L1+, above MAO human, reminders 2/5/10, expire 14, re-engage 60/90, decline requeue 90); D1 search profiles (one buy box → one profile → one search, supersede on new version); D2 matching with evidence, uncertainty, flags, pending review; mock property data with 7 subject fixtures incl. source conflicts, dirty comps, AVM divergence and pre-send drift |
| BO-12 Contracts + disposition | `contracts` table (migration 0003) where `signed` and every earnest-money transition require a human actor (automated actors are refused in the repository, tested); DocuSign interface + mock + live client that only ever creates drafts, never auto-sends; S6 disposition with buyer tiering and a blast template guarded against implying seller agreement; `DispositionWorkflow` with deadline alerts T‑3d/T‑1d/T‑0 for option period, inspection and closing, deal-death handling, realized fee on close; `/deals` operator page for every human step; contract KPIs |

## Worked offer example (fixture, Houston, Medium condition, roof disclosed, asking $100k)

7 tier‑1 comps → ARV $240,161 (High) · repairs $60,000 · MAO $95,169 · offer **$88,500** · all 7 gates pass · V5 recompute matches · at L1: V6 re-pull (0% drift), sent, negotiation started. Other fixtures: a 3‑way source conflict escalates to a human with confidence forced Low; dirty comps are all removed by V3 and the repair class bumped by V4; a V5 mismatch blocks.

## Tests

CI: 1,815 passed (SQLite + Postgres + 29 Temporal end-to-end workflow tests). ruff, ruff format, mypy --strict clean on 183 source files.

## What remains before go-live (unchanged in kind)

- Human checkpoints: PRE-07 Temporal Cloud and PRE-08 Railway (deploy + RUNBOOK restart test → 24/7 claim), PRE-05 Entra, PRE-04 domain warmup, PRE-01/02 LLC + address, PRE-16 copy/windows/disclosure legal review, PRE-10/11/15 data providers (the mocks are wired behind interfaces), PRE-13 DocuSign + attorney-reviewed templates, PRE-14 title company.
- Build: BO-13 eval harness over the golden sets (investor + seller), BO-14 seller synthetic pilot (mirror of the investor pilot through S1 → A7 → S5 → contract → S6), then the numeric placeholders in config/a7.yaml need real values before any offer is computed for a real seller.

## Decisions made this milestone (beyond v2)

1. Closing costs = 3% of pre-closing MAO (the "3% of offer" default is circular).
2. Repair line items roof/HVAC/foundation $12k/$8k/$10k are UNVALIDATED placeholders; Houston foundation always $12k contingency.
3. Trim = floor(n × 15%) each end keeping ≥3 comps (no trim below 7 comps).
4. Sensitive seller replies (dollar figures, "what's your offer", contract/legal terms) are forced to a human at every level.
5. No separate deadline workflow: timers live inside `DispositionWorkflow` so they die with the deal.
6. The A5 "under contract" prohibited-claim check is lifted for the buyer blast only when the contract row is signed.
7. DocuSign envelopes are created as drafts; the human sends and signs in DocuSign and then marks the contract signed in `/deals`.
8. Seller-thread routing: `GraphInboundMessageWorkflow` starts `SellerInboundWorkflow` as a child when the thread belongs to a seller lead.
