# REQUIREMENTS.md — Wholesaling Automation

Requirements matrix for the real estate wholesaling automation system. Generated 2026-10-25 from the three source documents in this folder. This file is the build contract: nothing is implemented that is not CONFIRMED here, and nothing CONFIRMED here is silently changed.

## How to read this file

**Statuses**

| Status | Meaning |
|---|---|
| CONFIRMED | A user decision. Build to it as written. |
| PROPOSED | Suggested by a document or by me, not yet decided by the user. Built only if you confirm, or built as a reversible default where noted. |
| REJECTED | Explicitly rejected or superseded. Not built. Reason given. |
| OPEN | Needs a user decision or an external fact. Where v2 gives a "default if you do not decide", the default is used until you decide. |

**Sources**

| Code | Document |
|---|---|
| MS | Wholesale_Automation_Master_Spec_v1.docx (product intent, v1.0, 2026-10-25) |
| BG | Claude_Code_Wholesaling_Build_Guide_v1.docx (original technical plan, v1.0, 2026-10-25) |
| V2 | Wholesaling Automation Architecture v2 (.docx and .pdf are identical content; 2026-10-25) |
| USR | Your build instructions pasted into Claude Code (same text as V2 "Prompt for Claude Code") |

**Precedence.** V2 supersedes BG and MS wherever they conflict (V2 "Decisions locked"). USR restates V2 and adds build-time rules; USR rules apply to this build. Where MS or BG contain a requirement that V2 does not contradict, it remains CONFIRMED and is listed with its original source.

**Build-time vs. design-time.** Several V2 features (SMS, DocuSign, live sends, promotion to L1) are CONFIRMED as *design* but must not *execute* during this build (USR rules). Those rows carry both facts: the feature is built behind an interface with a mock, and the live path stays disabled until the matching human-action checkpoint is cleared.

---

## 1. Mission and operating principles

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| GEN-01 | Build a lean, highly automated real estate wholesaling operation. | CONFIRMED | MS §1 | |
| GEN-02 | Not geographically restricted to Houston; multi-market from day one. | CONFIRMED | MS §1, §6; V2 Decisions | V2: markets selected by M1, cap 3 to 5 Active at launch. |
| GEN-03 | Investor demand drives acquisition: collect real buyer criteria first, then search for properties that fit buy boxes. | CONFIRMED | MS §1; V2 Build order | V2: seller outreach in a market waits for qualified buy boxes. |
| GEN-04 | Minimal overhead, clear ownership of each function, no unnecessary duplicate validation steps. | CONFIRMED | MS §1 | |
| GEN-05 | Google Sheets as the initial shared database. | REJECTED | MS §1 | Superseded by V2: PostgreSQL is the only writer of truth; Sheets is a one-way operator projection. See STK-02. |
| GEN-06 | Zapier for workflow orchestration. | REJECTED | MS §1 | Superseded by V2: Temporal is the workflow engine. Zapier survives only as the optional parser path (INT-20). |
| GEN-07 | ChatGPT Dots may be considered as an execution platform, pending confirmation of its capabilities. | REJECTED | MS §1, §7, §8; BG §0 | Superseded by V2: provider-agnostic model layer, default Anthropic, runtime is Temporal workers on Railway. Dots is not part of the architecture. |
| GEN-08 | Claude Code builds and tests the software; it is not the runtime. Workers on cloud infrastructure call the model API. | CONFIRMED | BG §0; V2 System architecture | "Claude Code builds and deploys it but never runs it." |
| GEN-09 | Virtual wholesaling, no boots on the ground: offers subject to inspection, seller-submitted photos, buyer due diligence. | CONFIRMED | V2 Decisions | |
| GEN-10 | Volume targets: 2,000 seller contacts/wk, 40 offers/wk, 4 contracts/mo, 2 to 3 assignments/mo at ~$10k average. | CONFIRMED | V2 Decisions | Targets, not build acceptance criteria. |
| GEN-11 | Buyer list starts from zero; investor funnel runs first. | CONFIRMED | V2 Decisions | |
| GEN-12 | Operator time is unlimited during L0; promotion to L1 is on quality metrics only. | CONFIRMED | V2 Decisions | |
| GEN-13 | Timeline: build sets the pace; ~4 weeks to L0 investor sends, seller side after. | CONFIRMED | V2 Decisions, roadmap | Target, not a commitment. Roadmap: Foundation wk 1; Investor agents wks 2 to 3; Investor L0 live wk 4; Seller agents wks 5 to 7; Seller L0 live wk 8+. |
| GEN-14 | Budget: no hard ceiling; soft cap plus alert in config. | CONFIRMED | V2 Decisions | Default soft cap in OPN-07. |
| GEN-15 | Treat the documents as the design baseline, not a claim that anything is deployed or connected. | CONFIRMED | MS §10; BG §0; V2 Data | |
| GEN-16 | Never claim a background task or 24/7 operation exists unless it verifiably does. | CONFIRMED | MS §10; BG §6; USR | 24/7 claim only after Railway deploy verified with a restart test. |

## 2. Stack and hosting (V2 locked decisions)

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| STK-01 | Python 3.12, FastAPI (webhook endpoint, web form, review queue API). | CONFIRMED | V2 Stack; USR | |
| STK-02 | PostgreSQL is authoritative state. Google Sheets is an operator projection, one-way sync from Postgres. Agents never write to Sheets. | CONFIRMED | V2 Decisions, Core principles | Overrides MS §1 and BG Step 4 "reconciliation from Sheets back to the database" (see V1R-06). |
| STK-03 | Temporal (Temporal Cloud) for all long-running sequences, schedules, signals, retries. Python SDK. | CONFIRMED | V2 Decisions; USR | |
| STK-04 | Railway hosts API container, worker containers, managed Postgres, webhook, web form; secrets in Railway env. Claude Code deploys via CLI. | CONFIRMED | V2 Decisions, Stack | |
| STK-05 | Microsoft Graph for Outlook mail (read, send, subscriptions). | CONFIRMED | V2 Stack; BG Step 3 | |
| STK-06 | Google Sheets API via service account, one-way write from the sync workflow. | CONFIRMED | V2 Integrations | |
| STK-07 | Model provider abstraction (`agents/llm.py`) with per-agent model routing in config. Default: Claude Haiku for classify/extract, Sonnet for drafting, QA, comp audit. Other providers (e.g. Grok) swappable per agent. | CONFIRMED | V2 Decisions, Stack; USR | USR: "provider-agnostic model layer defaulting to Anthropic". Exact model IDs: ENG-Q8. |
| STK-08 | SMS via Twilio or equivalent, only once A2P 10DLC registered. | CONFIRMED (design) / disabled this build | V2 Stack; USR | USR: no SMS at any point in this build. Interface + mock only. |
| STK-09 | Alerting via email digest plus optional Slack webhook. | CONFIRMED | V2 Stack | |
| STK-10 | Redis + Celery or a managed task queue for jobs. | REJECTED | BG §1 | Superseded by V2: Temporal. |
| STK-11 | Generic "cloud hosting with managed scheduler, secrets, logging and alerts"; cloud provider to be chosen later. | REJECTED | BG §1, §8 | Superseded by V2: Railway + Temporal Cloud. |
| STK-12 | Alternative stack choices require a written rationale. | CONFIRMED | BG §1 | Applied via section 21 (ENG-Q items). |
| STK-13 | Seller channels: email, SMS (draft-only until 10DLC + TCPA cleared), inbound web form. No calls, no direct mail, no answering service. | CONFIRMED | V2 Decisions | |

## 3. Cross-cutting principles

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| XC-01 | Every outbound action goes through the same code path at every autonomy level; the policy engine decides whether the last step is `send` or `queue_for_human`. | CONFIRMED | V2 Core principles; USR | |
| XC-02 | Every agent run is logged: prompt hash, model, tokens, cost, structured output, decision. | CONFIRMED | V2 Core principles; USR | Table `agent_runs`. |
| XC-03 | Idempotency keys on every inbound event (Graph `internetMessageId`, SMS SID, form submission id) and every outbound send. | CONFIRMED | V2 Core principles; USR; BG §1 | |
| XC-04 | Every outbound send is logged. | CONFIRMED | USR; BG §1 | Audit log on every send. |
| XC-05 | No autonomous contract execution, earnest-money transfer, binding negotiation, or property representation at any level. | CONFIRMED | V2 Core principles, Compliance; BG §5; USR | Hard floor in policy table. |
| XC-06 | Never embed credentials in agent prompts. | CONFIRMED | BG Step 3 | |
| XC-07 | Do not assert inventory, properties under contract, or exclusivity unless supported by records. | CONFIRMED | BG §0, Step 6; MS §3 | Enforced by A5 QA prohibited-claims check and locked templates. |
| XC-08 | Resilience: circuit breakers on model API and Graph; dead-letter queue with retry and alert after 3 failures; workflows replay safely after worker restart. | CONFIRMED | V2 Temporal | |
| XC-09 | Daily reconciliation compares Outlook, Postgres, Sheets; alert on mismatch; never silently overwrite. | CONFIRMED | V2 Schedules; BG §4 | |
| XC-10 | Do not invent existing API keys, subscriptions, accounts, permissions, or connected services; present setup checkpoints requiring user action. | CONFIRMED | BG §0; USR; V2 Data | `.env.example`, `SETUP.md`, checkpoint per service. |
| XC-11 | Every integration is built behind an interface with a mock implementation so tests and the synthetic pilot run without real services. | CONFIRMED | USR; V2 Build order ("mock data provider") | |

## 4. Autonomy policy

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| POL-01 | Autonomy is a per-action policy stored in a `policies` table, not a pilot phase. Code path identical at every level; only the final step changes. | CONFIRMED | V2 Autonomy policy; USR | |
| POL-02 | Every action defaults to L0. | CONFIRMED | USR | |
| POL-03 | Policy table implemented exactly as written (rows below). | CONFIRMED | V2 Autonomy policy; USR | |
| POL-03a | Investor initial outreach: L0 Draft, human sends / L1 QA-approved auto-send, daily cap / L2 Auto-send, weekly review. | CONFIRMED | V2 | |
| POL-03b | Investor follow-ups: L0 Draft / L1 Auto if initial was approved / L2 Auto. | CONFIRMED | V2 | |
| POL-03c | Reply to interested investor: L0 Draft / L1 Auto if QA confidence ≥ threshold and no price/contract/legal content / L2 Auto. | CONFIRMED | V2 | QA confidence threshold value: OPEN (OPN-10). |
| POL-03d | Seller outreach (email): L0 Draft / L1 Auto, daily cap / L2 Auto. | CONFIRMED | V2 | |
| POL-03e | Seller outreach (SMS): L0 Off / L1 Draft-only until 10DLC + consent cleared / L2 Auto with consent record. | CONFIRMED | V2 | USR: SMS never sends in this build. |
| POL-03f | Seller intake replies: L0 Draft / L1 Auto / L2 Auto. | CONFIRMED | V2 | |
| POL-03g | Offer generation and send: L0 Draft / L1 Auto if all A7 gates pass / L2 Auto. | CONFIRMED | V2 | |
| POL-03h | Counter acceptance: L0 Human / L1 Auto within negotiation room / L2 Auto within room. | CONFIRMED | V2 | |
| POL-03i | Contract execution: Human, always, at every level. | CONFIRMED | V2 | Hard floor. |
| POL-03j | Earnest money: Human, always, at every level. | CONFIRMED | V2 | Hard floor. |
| POL-03k | Buyer disposition blast: L0 Draft / L1 Auto for approved deals / L2 Auto. | CONFIRMED | V2 | |
| POL-03l | Opt-out suppression: Auto at every level. | CONFIRMED | V2 | |
| POL-04 | Promotion L0 → L1, per action: ≥50 human-approved drafts with ≤5% requiring edits; zero QA-blocked drafts sent in the window; zero compliance flags. | CONFIRMED | V2 | Promotion itself is a human-visible event; this build never promotes anything (USR: no live messages). |
| POL-05 | Demotion: any compliance incident, or edit rate >15% over 7 days, drops the action one level automatically and alerts you. | CONFIRMED | V2 | |
| POL-06 | Hard floors: contract, earnest money, any dollar commitment outside A7's formula, and any legal question stay human at every level. | CONFIRMED | V2 | |
| POL-07 | Daily policy promotion check schedule applies promotion/demotion criteria. | CONFIRMED | V2 Schedules | |
| POL-08 | "Pilot in draft-only mode" as a project phase; "require operator approval in pilot". | REJECTED | BG Step 3, Step 6, §6 | Superseded by V2: draft-only is L0 of the per-action policy table, not a phase. Behaviour is equivalent at L0. |
| POL-09 | Manual escalation required for ambiguous legal, price, contract, or commitment questions. | CONFIRMED | BG Step 7; V2 Hard floors | |

## 5. Agent roster

16 agents. Each is a Temporal activity with a fixed system prompt, tool definitions, JSON output schema, and a model routed by config. A5 QA and the policy engine are shared across both funnels (V2 Agent roster).

| ID | Agent | Side | Input → output | Default model | Status | Source |
|---|---|---|---|---|---|---|
| AGT-M1 | M1 Market Selection | Shared | Market signals → ranked markets (Active / Watch / Excluded) | Haiku + code | CONFIRMED | V2 |
| AGT-A1 | A1 Investor Discovery | Investor | Deed records in a market → scored prospects | Haiku + code | CONFIRMED | V2; MS §2; BG §2 |
| AGT-A2 | A2 Contact Research | Investor | Prospects → professional contact channels with provenance | Haiku | CONFIRMED | V2; MS §2; BG §2 |
| AGT-A3 | A3 Outreach | Investor | Template + contact → initial + 3 follow-ups in one thread | Sonnet | CONFIRMED | V2; MS §2; BG §2 |
| AGT-A4 | A4 Reply & Buy Box | Investor | Inbound reply → classification, draft reply, extracted criteria | Sonnet | CONFIRMED | V2; MS §2; BG §2 |
| AGT-A5 | A5 QA | Shared | Any outbound draft + context → approve / block / rewrite | Sonnet | CONFIRMED | V2; MS §2; BG §2 |
| AGT-A6 | A6 Parser Formatter | Investor | Confirmed buy box → Zapier parser email (flag off by default) | Haiku | CONFIRMED (behind flag) | V2; MS §2; BG §2 |
| AGT-D1 | D1 Search Profile | Deal | Buy box event → versioned search profile | Code | CONFIRMED | V2; MS §5; BG §2 |
| AGT-D2 | D2 Property Match | Deal | Profile + sources → ranked candidates with evidence | Sonnet + code | CONFIRMED | V2; MS §5; BG §2 |
| AGT-S1 | S1 Seller List Builder | Seller | Market + distress criteria → seller leads | Code | CONFIRMED | V2 |
| AGT-S2 | S2 Skip Trace | Seller | Owner → email, phone, mailing address with provenance | Code + vendor | CONFIRMED | V2 |
| AGT-S3 | S3 Seller Outreach | Seller | Template + lead → email/SMS sequence | Sonnet | CONFIRMED | V2 |
| AGT-S4 | S4 Seller Intake | Seller | Inbound reply / form → motivation, condition, timeline, asking, photos | Sonnet | CONFIRMED | V2 |
| AGT-A7 | A7 Offer Engine | Seller | Complete intake → verified offer + full calc record | Sonnet + code | CONFIRMED | V2 |
| AGT-S5 | S5 Negotiation | Seller | Seller response → counter handling, cadence, expiry, re-engage | Sonnet | CONFIRMED | V2 |
| AGT-S6 | S6 Disposition | Deal | Signed contract → buyer blast to D2 matches, deposit, closing coordination | Sonnet | CONFIRMED | V2 |
| AGT-X1 | Five independent lead-source bots (pre-foreclosure, tax delinquency, absentee, probate, stale listings) as the primary acquisition strategy. | — | — | — | REJECTED | MS §7; BG §2 D1 rule. Rejected by user. Those signals are used only as S1 list criteria inside the buy-box-driven, M1-gated seller funnel (SEL-01). |
| AGT-X2 | Separate email-verification / deliverability bot. | — | — | — | REJECTED | MS §2, §7; BG §2 A2 rule. Rejected by user. Bounce handling feeds suppression instead (INT-26). |
| AGT-X3 | Complex investor vetting layer. | — | — | — | REJECTED | MS §7. Rejected by user. A1 scoring (INV-04) is the only qualification. |

## 6. M1 market selection

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| M1-01 | Markets chosen by investor demand, re-scored monthly, promoted Watch → Active only once qualified buy boxes exist there. Seller outreach never runs ahead of buyer demand. | CONFIRMED | V2 M1 | |
| M1-02 | Signals per metro or county with weights: investor demand 0.30; spread 0.25; distress supply 0.20; liquidity 0.10; price band fit 0.10; legal friction −0.05 (state rules scored 0 to 3 from a config table); data availability is a gate. | CONFIRMED | V2 M1 | Signal measures as in V2 table. |
| M1-03 | `market_score = 0.30 demand + 0.25 spread + 0.20 distress + 0.10 liquidity + 0.10 price_fit − 0.05 friction`, after the data gate. Weights live in config and are retuned from realized results. | CONFIRMED | V2 M1 | |
| M1-04 | Tiers: Active (top N, cap 3 to 5 at launch, both funnels run); Watch (investor discovery and outreach only; promoted to Active at ≥10 qualified buy boxes); Excluded (failed legal or data gate). | CONFIRMED | V2 M1 | |
| M1-05 | Feedback loop: once live, proxy signals replaced by own results per market (buy-box count, reply rate, offers accepted, assignments closed). Market drops to Watch if 90-day results fall below threshold. | CONFIRMED | V2 M1 | Threshold value: OPEN (OPN-11). |
| M1-06 | Deal band per market: global default $75k to $350k purchase (ARV ~$120k to $500k); M1 may set a market's band at median × 0.5 to × 1.5, clamped to the global range. | CONFIRMED | V2 Decisions, M1 | |
| M1-07 | Monthly rescore schedule; promotion/demotion requires the threshold crossed twice. | CONFIRMED | V2 Schedules | |
| M1-08 | Market scoring proposal (legal friction; buyer demand; discounted inventory; spread after repairs/costs; data availability; competition; remote operational ease). | REJECTED | MS §6 | Superseded by V2 M1 weighted formula (M1-02/03). "Competition" and "operational ease" are not in V2's formula. |
| M1-09 | Earlier candidate markets (Indianapolis, Atlanta, Birmingham, Jacksonville, Houston, Cleveland) are preliminary only; no ranking approved. | CONFIRMED | MS §6 | V2 seeds M1 with Houston plus 20 largest Sunbelt/Midwest metros by default (OPN-08). |
| M1-10 | Do not treat low purchase price or high foreclosure volume alone as proof a market is profitable. | CONFIRMED | MS §6 | Encoded by the weighted formula and data gate. |
| M1-11 | Approve first Active markets from M1's initial run. | OPEN (human checkpoint) | V2 Pre-launch | |

## 7. Investor funnel (A1 to A6)

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| INV-01 | Find investors buying in an Active or Watch market regardless of where they live. Avoid burdensome qualification. | CONFIRMED | MS §2; BG §2; V2 Investor funnel | |
| INV-02 | A1 qualification (deed records): buyer is an entity (LLC, LP, trust, corp) or an individual with ≥2 purchases in 24 months; cash or hard-money purchase; residential 1 to 4 unit (multifamily and land configurable later); purchase price inside market deal band; last purchase ≤18 months. | CONFIRMED | V2 | |
| INV-03 | Investor types: Flipper (resale 4 to 12 mo at higher price) Priority; Buy-and-hold (>12 mo, clustered ZIP) Priority; Wholesaler/competitor (assignment or double-close patterns, high velocity) Deprioritize; iBuyer/institutional (known entity list) Exclude; Agent-investor (license match) Flag, include. | CONFIRMED | V2 | |
| INV-04 | A1 score 0 to 100: volume 30, recency 25, cash rate 15, price-band fit 15, market concentration 10, type bonus ±5. ≥50 → prospect, A2 runs. ≥75 → priority tier, first in outreach queue. | CONFIRMED | V2 | |
| INV-05 | Exclusions: institutional, government, nonprofits, lenders taking REO, builders buying lots, suppression list. | CONFIRMED | V2 | |
| INV-06 | Entity resolution: collapse LLC variants and shared mailing addresses into one investor record; registered agent or mailing address feeds A2. Every source deed kept as evidence. | CONFIRMED | V2; BG Step 5 | Merge conservatively, keep source evidence (BG). |
| INV-07 | A1 captures investor name/entity, source link, market evidence, transaction dates where verified, investor type, confidence/unknown fields. | CONFIRMED | BG Step 5 | Not contradicted by V2. |
| INV-08 | A2 finds publicly available professional contact details or licensed enrichment data; documents provenance and use limitations; dedupes and removes junk. | CONFIRMED | MS §2; BG Step 5; V2 | |
| INV-09 | A2 output includes phone numbers. | CONFIRMED (capture only) | MS §2 | Phone captured for the record; no calls or SMS to investors in V2 (investor channel is Outlook only). |
| INV-10 | A3: approved Houston template parameterized by market and first name; initial + 3 follow-ups at ~4-day intervals in the same Graph conversation; subject-line tests on the initial only; any substantive reply, opt-out, or pause cancels remaining sends. | CONFIRMED | MS §2, §3; BG Step 6; V2 | Interval default OPN-04. |
| INV-11 | Subject-line experimentation tests relevance and qualified-response rate, not deceptive urgency; do not repeatedly present lists of subject options to the operator. | CONFIRMED | MS §3; BG Step 6 | |
| INV-12 | Throttles, business-hour send windows, delivery-error handling on outreach. | CONFIRMED | BG Step 6; V2 Pre-launch (send windows) | Send windows and caps: human approval (PRE-16). |
| INV-13 | A4 classification: interested, buy box supplied, question, not now, not interested, opt-out, ambiguous, unrelated. | CONFIRMED | BG Step 7; V2 | |
| INV-14 | A4 for interested replies: extract explicit criteria; ask only for missing information that matters; preserve entire thread context; never invent criteria; track unknowns; corrections overwrite with history. | CONFIRMED | BG §2, Step 7; V2 | |
| INV-15 | A4 for not-now: capture preferred reconnect date if offered. For opt-outs: suppress immediately, cancel queued sends across all campaigns. | CONFIRMED | BG Step 7, §4; V2 | |
| INV-16 | Investor inbound path: Graph webhook or 15-min delta poll → dedupe → A4 classify → draft → A5 QA → policy → send or queue → audit. | CONFIRMED | V2 Event paths; BG §1 | |
| INV-17 | A5 QA reviews every outgoing draft (both funnels) with previous thread messages, facts known, latest buy box, and marketing rules; checks thread context, accuracy, compliance, cadence, performance; outputs approve / block / rewrite. | CONFIRMED | MS §2; BG §2, Step 7; V2 | |
| INV-18 | A5 blocks drafts with contradictory facts, invented deals, inappropriate promises, wrong recipient, repeated requests, prohibited claims, or opt-out violations. Review results recorded in `qa_reviews`. | CONFIRMED | BG Step 7 | |
| INV-19 | A5 tracks qualified buy boxes, response and opt-out rates; improves messaging. | CONFIRMED | MS §2 | Surfaces in daily digest. |
| INV-20 | A6: convert confirmed buy box to a labeled parser email with investor_id, buy_box_id, version, source_message_id and all structured fields; send to Zapier Email Parser; Zap upserts the Sheets row (lookup + create/update branches if direct upsert unavailable). | CONFIRMED, behind `ZAPIER_PARSER_ENABLED=false` | MS §2; BG Step 4; V2 | See INT-20. |
| INV-21 | Buy-box path: A4 extract → Postgres upsert (versioned) → `buy_box.updated` → D1 search profile → D2 match. | CONFIRMED | V2 Event paths | Overrides BG §1 buy-box path through parser/Zapier/Sheets (V1R-05). |
| INV-22 | A4/A5 tests must include sarcasm, corrections, multiple buy boxes, quoted old email text, opt-out requests, out-of-office replies. | CONFIRMED | BG Step 7 | Golden test sets per agent (BO-13). |
| INV-23 | Acceptance: all four messages appear as one Outlook conversation; a reply cancels every future send. | CONFIRMED | BG Step 6 | |
| INV-24 | Acceptance: one inbound test message produces exactly one logged event; drafted reply retains correct conversation context. | CONFIRMED | BG Step 3 | |

## 8. Approved investor outreach copy

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| CPY-01 | Initial outreach body is approved verbatim and must be retained unless you explicitly change it: "Hi [First Name], / We're always working on new wholesale real estate opportunities throughout the Houston area and are looking to connect with investors interested in off-market deals. / Are you currently purchasing properties in Houston? If so, would you mind sharing your current buy box so we can send opportunities that match your investment criteria? / Best, / [Your Name]" | CONFIRMED | MS §3 | Stored as a locked template file, not regenerated by a model. |
| CPY-02 | The copy was approved for Houston. For other markets replace "Houston" with the actual target market; never imply existing inventory or contracts. | CONFIRMED | MS §3; BG Step 6; V2 | Template parameter `{market}`. |
| CPY-03 | Three follow-ups ~4 days apart in the same thread; cancel on reply. | CONFIRMED | MS §3 | |
| CPY-04 | Final follow-up tone: "Did we miss the mark, or is now simply not a good time? Let us know when would be better to reconnect." Guidance, not approved verbatim copy. | PROPOSED | MS §3 | Follow-up bodies need your approval (PRE-16). I will draft them and queue for approval. |
| CPY-05 | Honor opt-outs and applicable commercial-email, telephone and text rules. | CONFIRMED | MS §3 | See section 14. |

## 9. Buy boxes, D1 search profiles, D2 matching

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| BB-01 | Buy box captures: property type, preferred ZIPs / neighborhoods / markets, budget or price range, condition tolerance, financing type, closing timeline, any other explicitly stated criteria. | CONFIRMED | MS §4; BG Step 2 | Structured fields plus original raw text. |
| BB-02 | Identifiers: stable investor_id; separate buy_box_id per distinct criteria set; source/message reference; timestamps; last-updated; active/inactive status; version number. Deterministic upserts; no duplicate profiles. | CONFIRMED | MS §4; BG Step 2 | |
| BB-03 | Same investor can have multiple buy boxes; updates never create accidental duplicates. Test duplicate, update, correction, and conflicting-update scenarios. | CONFIRMED | BG Step 2 | Acceptance for schema step. |
| BB-04 | Trigger: whenever a new or updated buy box exists, convert it into a usable, versioned search profile linked to the buy_box_id; normalize free text to structured fields retaining original wording and unknowns. | CONFIRMED | MS §5; BG Step 8; V2 | Detection via Postgres event, not Zapier/Sheets (V1R-07). |
| BB-05 | Queue a property search when a profile is created or meaningfully changed; coalesce duplicate events; changes update the existing profile rather than creating an unrelated one; avoid duplicate search jobs for the same revision. | CONFIRMED | MS §5; BG Step 8; V2 BuyBoxSearch workflow | `BuyBoxSearch` workflow is superseded by a newer version via signal. |
| BB-06 | Acceptance: entering a new buy box creates one active search profile and queues exactly one initial search. | CONFIRMED | BG Step 8 | |
| BB-07 | D2 queries approved sources within each profile; dedupes listings/parcels by stable IDs and addresses; ranks by buy-box fit and confidence with separate estimates for ARV, repairs, fees, assignment spread, liquidity. | CONFIRMED | BG Step 9; V2 | |
| BB-08 | D2 flags title, liens, HOA, financing constraints, data gaps for manual due diligence; never implies a seller has agreed to sell; logs source, retrieval time, match logic, missing fields, source restrictions, freshness, valuation uncertainty, next review action. | CONFIRMED | BG Step 8, 9, §2; V2 | |
| BB-09 | Matches go to a review surface; only approved deals may be described to buyers as available. Every suggested property shows evidence, reason for match, uncertainty, human-review status. | CONFIRMED | MS §5; BG Step 9 | Review surface = review queue page (BO-07). |
| BB-10 | Value, title/ownership, deal economics and seller intent are checked before presenting any property as an actionable deal. | CONFIRMED | MS §5 | A7 verification covers value/economics; title and intent are flags/human review. |
| BB-11 | Track response, contract status, actual closings and realized assignment economics for optimization. | CONFIRMED | BG Step 9; V2 M1 feedback loop | |
| BB-12 | D2 listing sources: same as comps providers, plus off-market from A7 contracts. | CONFIRMED | V2 Data providers | |

## 10. Seller funnel (S1 to S6)

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| SEL-01 | Seller outreach starts in a market only after it is Active. | CONFIRMED | V2 | |
| SEL-02 | S1 list criteria (per Active market, refreshed weekly): absentee owner, tax delinquent, pre-foreclosure, probate, code violations, long-term hold (≥15 yrs), vacant, expired listing; residential 1 to 4 unit; inside deal band on assessed or estimated value; score by stacked signals; ≥2 signals → outreach queue. | CONFIRMED | V2 | |
| SEL-03 | S2 skip trace via vendor API; provenance and confidence per contact; suppress DNC and prior opt-outs before any SMS. | CONFIRMED | V2 | Mock vendor until selected (PRE-11). |
| SEL-04 | S3 sequence: email initial + 3 follow-ups at 3/7/14 days; SMS only once 10DLC registered and consent basis documented. | CONFIRMED | V2 | Intervals default OPN-05. SMS disabled this build. |
| SEL-05 | Every seller-facing message carries the TX equitable-interest disclosure, sender identity, physical address, and opt-out. | CONFIRMED | V2 | Address and identity: human checkpoints PRE-02, PRE-16. |
| SEL-06 | S4 intake fields: property address (parcel-verified) required; motivation required; condition class (Light/Medium/Heavy/Gut) + disclosed issues required; timeline required; asking price optional; photos requested (web form upload link sent automatically; offer proceeds without, confidence drops one level); co-owners, occupancy, liens, HOA asked, unknowns flagged. | CONFIRMED | V2 | |
| SEL-07 | Intake is conversational: S4 asks only for missing required fields, one or two per message; marks `intake_complete` when all required fields present; that event triggers A7. | CONFIRMED | V2 | |
| SEL-08 | Seller inbound path: email / SMS / web form → dedupe → S4 intake → if complete → `offer.requested` → A7. | CONFIRMED | V2 Event paths | |
| SEL-09 | S5: auto-accept counters within negotiation room (offer to MAO); above MAO → human. Reminders days 2/5/10; expire at 14; re-engage at 60/90. Declines capture reason and requeue at 90 days. | CONFIRMED | V2 | Counter acceptance is Human at L0 (POL-03h). |
| SEL-10 | S6 (after human-signed contract): blast to D2-matched buyers by tier order; collect proof of funds and deposit terms; assignment agreement via DocuSign; title/escrow coordination; deadline alerts (option period, inspection, closing); deal-death handling. | CONFIRMED (design) / DocuSign mocked this build | V2 | USR: no contract automation. DocuSign behind interface + mock. |
| SEL-11 | Deal path: A7 offer accepted → `contract.requested` → human signs → S6 disposition. | CONFIRMED | V2 Event paths | |
| SEL-12 | Web form: FastAPI page on Railway, photo upload to object storage, submission id as idempotency key. | CONFIRMED | V2 Integrations | Object storage choice: ENG-Q9. |

## 11. A7 offer engine

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| A7-01 | Every rule is numeric; an offer auto-sends only when every verification pass and every gate passes; the full calculation is logged with it. | CONFIRMED | V2 A7; USR | |
| A7-02 | Pipeline step 1, property resolution: address → parcel ID; beds/baths/sqft/year/lot from county and provider. | CONFIRMED | V2 | |
| A7-03 | Step 2, comp pull: sold comps same ZIP, ±20% sqft, ±15 yrs, ≤180 days, ≤0.75 mi (tier 1). Expand only if fewer than 3: tier 2 = 1.0 mi / 270 d; tier 3 = 1.5 mi / 365 d. Record tier used. | CONFIRMED | V2 | |
| A7-04 | Step 3, ARV: comp price/sqft, trim top and bottom 15%, weight by recency and distance, × subject sqft. Confidence: High ≥5 comps, tier 1, spread ≤12%; Medium ≥3 comps, tier ≤2, spread ≤20%; Low anything else → human. | CONFIRMED | V2 | Exact recency/distance weighting function is not specified: ENG-Q10. |
| A7-05 | Step 4, repairs: condition class × per-sqft rate table (config, per market) + disclosed line items (roof, foundation, HVAC). Foundation in Houston always adds a fixed contingency. | CONFIRMED | V2 | Rate table values and Houston contingency amount: OPEN (OPN-12). |
| A7-06 | Step 5, MAO = ARV × market_pct − repairs − assignment_fee − closing_costs. market_pct default 0.70 per market; fee = target per deal; closing = fixed % table. | CONFIRMED | V2 | Defaults OPN-01, OPN-02; closing % table OPN-13. |
| A7-07 | Step 6, offer = MAO minus negotiation room (default 7%), rounded down to $500. | CONFIRMED | V2 | Default OPN-03. |
| A7-08 | Step 7, sanity gates, all required for auto-send: ARV confidence ≥ Medium; offer ≤ market hard cap; offer ≥ 35% of ARV; offer within ±25% of asking or asking blank; no open flags (title/lien, HOA, multi-unit, non-residential, probate without authority, co-owner missing); seller not suppressed; no active offer on same parcel. | CONFIRMED | V2; USR | Implemented as a gate list evaluated in code, each result persisted. |
| A7-09 | Step 8, draft: locked template with number, "subject to inspection and signed agreement," cash close in N days, no fee disclosure, no buyer-lined-up claims. A5 QA checks facts and prohibited claims. | CONFIRMED | V2 | N days: OPEN (OPN-14). |
| A7-10 | Step 9, send per policy level with full calc record attached. Step 10, hand to S5. | CONFIRMED | V2 | L0 = Draft, human sends. |
| A7-11 | Verification loops run between steps 6 and 7 with fixed max iterations so the workflow always terminates. | CONFIRMED | V2 | V6 is defined as "immediately before send", so it runs after draft/QA and re-runs the gates; see ENG-Q11 for the ordering I intend to implement. |
| A7-12 | V1 Dual-source facts: county vs provider on sqft, beds, baths, year, lot. >10% gap → pull third source; majority wins; none → human. | CONFIRMED | V2; USR | |
| A7-13 | V2 Dual-method ARV: comp calc vs provider AVM. >10% gap → re-run comps at next tier, re-compare; still >10% → confidence −1. Max 3 passes. | CONFIRMED | V2; USR | |
| A7-14 | V3 Comp audit: model flags non-arm's-length, new build, already-renovated flip, lot-value outlier. Remove flagged, recompute; loop until no new flags or 2 iterations. | CONFIRMED | V2; USR | Sonnet (comp audit). |
| A7-15 | V4 Repair cross-check: intake class vs year built, last sale, listing photos/remarks. Evidence worse than stated → bump class one level, log reason. | CONFIRMED | V2; USR | |
| A7-16 | V5 Independent recompute: separate function recalculates MAO and offer from logged inputs; must match to the dollar; mismatch → block, alert (code bug). | CONFIRMED | V2; USR | |
| A7-17 | V6 Pre-send drift: re-pull comps immediately before send; any new comp moving ARV >5% → recompute, re-run gates. | CONFIRMED | V2; USR | |
| A7-18 | Verification record attached to every offer and persisted: sources used, comps kept and rejected with reasons, ARV from both methods, repair class and any adjustment, every gate result. At L1 you review records weekly, not individual offers. | CONFIRMED | V2; USR | Table `offer_verifications`. |
| A7-19 | Define deal underwriting thresholds, acceptable margins, escalation to a human, and who can negotiate or sign contracts. | CONFIRMED | MS §8 | Resolved by V2 A7 numbers, policy table, and hard floors. Signing: human only. |

## 12. Temporal workflows, schedules, triggers

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| WF-01 | Every multi-day sequence is a durable workflow; every recurring job is a Temporal schedule; every interrupt is a signal. Nothing depends on cron or a laptop. | CONFIRMED | V2 | |
| WF-02 | `InvestorSequence`: started by A2 qualified contact; sleeps 4 d between sends; signals reply, opt_out, pause, bounce. | CONFIRMED | V2 | |
| WF-03 | `SellerSequence`: started by S1/S2 queued lead; sleeps 3/7/14 d; signals reply, opt_out, pause, intake_complete. | CONFIRMED | V2 | |
| WF-04 | `OfferWorkflow`: started by intake_complete; no sleeps (runs verification then hands off); signal human_decision. | CONFIRMED | V2 | |
| WF-05 | `NegotiationWorkflow`: started by offer sent; sleeps 2/5/10 d, expire 14, re-engage 60/90; signals counter, accept, decline, human_override. | CONFIRMED | V2 | |
| WF-06 | `DispositionWorkflow`: started by contract signed; sleeps per deadline; signals buyer_response, deposit_received, deal_dead. | CONFIRMED | V2 | |
| WF-07 | `BuyBoxSearch`: started by buy_box.created/updated; no sleeps; signal superseded (newer version). | CONFIRMED | V2 | |
| WF-08 | Schedule: M1 market rescore, monthly; promotion/demotion requires threshold crossed twice. | CONFIRMED | V2 | |
| WF-09 | Schedule: A1 investor discovery, daily 06:00 local per market; no overlapping runs; rate limits per provider. | CONFIRMED | V2 | V2 fixes 06:00; BG's "illustrative" caveat is superseded. |
| WF-10 | Schedule: S1 seller list refresh, weekly; dedupe against existing leads and suppression. | CONFIRMED | V2 | |
| WF-11 | Schedule: Graph subscription renewal every 48 h; alert if renewal fails twice. | CONFIRMED | V2; BG Step 3 | |
| WF-12 | Schedule: Graph delta poll every 15 min; reconciles missed webhooks; idempotent. | CONFIRMED | V2; BG Step 3 | |
| WF-13 | Schedule: Sheets sync every 15 min, one-way Postgres → Sheets. | CONFIRMED | V2 | |
| WF-14 | Schedule: daily reconciliation (Outlook, Postgres, Sheets); alert on mismatch; never silently overwrite. | CONFIRMED | V2; BG §4 | |
| WF-15 | Schedule: policy promotion check, daily. | CONFIRMED | V2 | |
| WF-16 | Schedule: budget check hourly; soft cap hit → pause discovery and search, keep inbound handling. | CONFIRMED | V2 | |
| WF-17 | Schedule: watchdog every 10 min; stale heartbeats → alert. | CONFIRMED | V2 | |
| WF-18 | 07:00 local "review due outreach and QA drafts" schedule. | REJECTED | BG §4 | Not in V2's schedule table; BG itself marks the time illustrative. Replaced by the daily digest (BO-07) and the review queue, which are not time-gated. Re-add if you want a fixed review time. |
| WF-19 | Trigger on Outlook message received: dedupe by message ID; run A4 → A5 → reply/draft; stop sequence on reply. | CONFIRMED | BG §4; V2 | |
| WF-20 | Trigger on buy box extracted: persist source and version; retry failures; run D1 → D2 (and A6 → parser only when flag on). | CONFIRMED | BG §4; V2 | |
| WF-21 | Trigger on opt-out: immediate suppression; cancel queued sends across all campaigns. | CONFIRMED | BG §4; V2 | |
| WF-22 | Redeploy/restart test: pending sends are neither lost nor duplicated; workflows recover from worker restart. | CONFIRMED | BG Step 10; V2 Resilience; USR | Gate for any 24/7 claim. |
| WF-23 | Health checks, restart policies, dead-letter queues; alerts for webhook expiration, failed syncs, parser errors, stuck queues, bounced emails, agent QA failures. | CONFIRMED | BG Step 10; V2 | |

## 13. Data providers and integrations

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| INT-01 | No data provider, entity, or sending identity exists yet; each is a build step with selection criteria, not an assumption. | CONFIRMED | V2 Data; BG §0 | |
| INT-02 | Provider selection criteria: coverage in Active markets, API access, cost per record, license permits automated use. | CONFIRMED | V2 | |
| INT-03 | Deed / transaction records: evaluate county APIs, ATTOM, PropStream, BatchData. Feeds M1, A1, S1. | OPEN (selection) | V2 | Interface `DeedRecordsProvider` + mock. |
| INT-04 | Distress lists: evaluate PropStream, BatchLeads, county tax/probate. Feeds S1. | OPEN (selection) | V2 | Interface + mock. |
| INT-05 | Skip trace: evaluate BatchSkipTracing, Tracerfy, PropStream. Feeds S2. | OPEN (selection) | V2 | Interface + mock. |
| INT-06 | Comps and AVM: evaluate ATTOM, HouseCanary, RentCast, MLS via licensed partner. Feeds A7, D2. | OPEN (selection) | V2 | Interface + mock. |
| INT-07 | Property-data terms: determine access, costs, search limits, terms of use; never assume search access exists. | CONFIRMED | MS §8; BG Step 8 | |
| INT-10 | Microsoft Graph: app registered in Entra ID with least-privilege permissions; tokens in secrets manager; confirm mailbox is personal or Microsoft 365 business. | CONFIRMED | V2; BG Step 3 | Human checkpoint PRE-05. |
| INT-11 | Graph: OAuth/consent, secure token storage and renewal. | CONFIRMED | BG Step 3 | |
| INT-12 | Graph: subscribe to message change notifications with polling fallback; renewal; missed-event reconciliation; validate webhook handshake; fetch message content and conversationId; detect inbound replies and delivery failures. | CONFIRMED | BG Step 3; V2 | |
| INT-13 | Graph: draft creation and sending are separate operations. | CONFIRMED | BG Step 3; V2 Build order | |
| INT-14 | Google Sheets: service account; stable investor_id and buy_box_id on every row; tabs Investors, Contacts, Buy Boxes, Search Profiles, Properties, Matches, Conversations, Suppressions, QA Log (plus Outreach Events per MS). | PROPOSED (tab list) / CONFIRMED (sync) | MS §4; BG Step 4; V2 | MS marks the tab list a proposal. I will build the sync to these tabs unless you change them. |
| INT-15 | Sheets access via "service-account or authorized OAuth". | CONFIRMED as service account | BG Step 4; V2 | V2 fixes service account. |
| INT-16 | DocuSign: PSA and assignment templates pre-filled from deal record. | CONFIRMED (design) / mocked this build | V2 | Human checkpoint PRE-13; no contract automation this build. |
| INT-17 | SMS: Twilio or equivalent; A2P 10DLC brand and campaign registration before first send. | CONFIRMED (design) / disabled this build | V2 | PRE-12. |
| INT-18 | Alerting: email digest + optional Slack webhook. | CONFIRMED | V2 | |
| INT-20 | Zapier parser path exists behind `ZAPIER_PARSER_ENABLED`, default false. | CONFIRMED | V2 Decisions, Integrations; USR | |
| INT-21 | Zapier Email Parser mailbox and parser template with stable labels and explicit field delimiters; Zap upserts Sheets row. | CONFIRMED, only when flag on | BG Step 4 | Human checkpoint only if you enable the flag. |
| INT-22 | Acceptance: two parser emails changing one buy box update the same row; replaying a parser message causes no duplicate. | CONFIRMED, only when flag on | BG Step 4 | |
| INT-23 | "Keep Google Sheets and the Zapier parser in the workflow." | REJECTED (Zapier part) | BG §6 | Superseded by V2: Zapier optional, default off. Sheets stays (as projection). |
| INT-24 | Deliverability: dedicated sending domain(s) separate from the consulting domain; 2 to 3 week warmup before volume; SPF/DKIM/DMARC; domain rotation at scale. | CONFIRMED | V2 | Human checkpoint PRE-04. |
| INT-25 | Bounce and complaint handling feeds suppression. | CONFIRMED | V2 | |
| INT-26 | Web form photo upload to object storage. | CONFIRMED | V2 | Storage choice ENG-Q9. |

## 14. Compliance

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| CMP-01 | CAN-SPAM: truthful headers and subjects, sender identity, physical address, working opt-out in every email. | CONFIRMED | V2; BG §5 | |
| CMP-02 | TCPA / DNC: SMS off until 10DLC registered and consent basis documented per contact; DNC scrub before any SMS; assess federal/state DNC and carrier rules first. | CONFIRMED | V2; BG §5; MS §8 | |
| CMP-03 | Texas: equitable-interest disclosure in all seller-facing material; no advertising a property you do not own or control; operate unlicensed with TX disclosure. | CONFIRMED | V2 Decisions, Compliance | |
| CMP-04 | Per-market legal review (licensing, advertising equitable interests, disclosures, assignments, title/escrow, earnest-money risk) with qualified counsel before activating a new state. | CONFIRMED | V2; BG §5; MS §8 | Human checkpoint per new state. |
| CMP-05 | Privacy: minimize stored personal data, restrict access, document sources, honor removal requests, no scraping of prohibited or restricted sources. | CONFIRMED | V2; BG §5 | |
| CMP-06 | AI autonomy: no contracts, earnest money, binding negotiation, or property representations without human action. | CONFIRMED | V2; BG §5 | Same as XC-05. |
| CMP-07 | Opt-out/suppression handling, identity/address disclosures, TCPA/DNC safeguards for outbound marketing. | CONFIRMED | MS §8 | |
| CMP-08 | Entity/legal: LLC, address, bank, title company are pre-launch checklist items. | CONFIRMED | V2 Decisions | PRE-01..03, PRE-14. |

## 15. Data model

| ID | Requirement | Status | Source | Notes |
|---|---|---|---|---|
| DM-01 | Tables: markets, investors, contacts, buy_boxes (versioned), seller_leads, intakes, offers, offer_verifications, property_candidates, matches, messages, threads, sequences, policies, suppression, qa_reviews, agent_runs, audit_events. | CONFIRMED | V2 Build order | |
| DM-02 | UUID primary keys; source IDs; created/updated timestamps; unique email normalization; idempotency keys; buy-box version numbers. | CONFIRMED | BG Step 2 | Not contradicted by V2. |
| DM-03 | Buy-box geography, asset types, price ranges, condition, strategy, financing and closing preferences as structured fields plus original raw text. | CONFIRMED | BG Step 2; MS §4 | |
| DM-04 | Migrations exist and are tested for duplicate, update, correction, and conflicting-update scenarios. | CONFIRMED | BG Step 2 | |
| DM-05 | `search_profiles` table (versioned, linked to buy_box_id). | PROPOSED | BG Step 2; V2 D1 | V2's table list omits it, but V2 D1 produces "versioned search profiles" and the Sheets tab list includes it. I will add it unless you object. |
| DM-06 | `source_evidence` table. | PROPOSED | BG Step 2; V2 A1 | V2 requires "every source deed is kept as evidence" but names no table. I will add it unless you prefer evidence columns on investors/seller_leads. |
| DM-07 | `campaigns` and `scheduled_sends` tables. | REJECTED | BG Step 2 | Superseded by V2 `sequences` plus Temporal workflow state (sends are workflow timers, not rows to poll). Send history lives in `messages`. |
| DM-08 | `consent_suppression` table name. | REJECTED (renamed) | BG Step 2 | V2 names it `suppression`. Same content. |
| DM-09 | `property_matches` table name. | REJECTED (renamed) | BG Step 2 | V2 names it `matches`. |
| DM-10 | `policies` table holds per-action autonomy level plus caps and thresholds. | CONFIRMED | V2 | |
| DM-11 | Config-held tunables (M1 weights, legal friction scores, repair rate tables, closing % table, deal bands, soft budget cap) are versioned config, not hard-coded. | CONFIRMED | V2 (weights live in config; config table) | Format: ENG-Q6. |

## 16. Build order (V2) and build-time rules

Roadmap: five phases, four gates (V2 diagram).

| Phase | Weeks | Scope | Gate to next phase |
|---|---|---|---|
| Foundation | 1 | Repo, schema, Temporal, Outlook, Railway | tests green |
| Investor agents | 2 to 3 | M1, A1 to A5, policy, review queue, data pick | synthetic pilot |
| Investor L0 live | 4 | Real sends, you approve, buy boxes captured | ≥10 buy boxes |
| Seller agents | 5 to 7 | S1 to S6, A7, D1, D2, web form, DocuSign | LLC + 10DLC |
| Seller L0 live | 8+ | First offers; promote to L1 on metrics | — |

Domain warmup and LLC filing start in week 1 so neither blocks week 4.

| ID | Step | Status | Source | Notes |
|---|---|---|---|---|
| BO-01 | Repo, CI, `.env.example`, requirements matrix (this file). | CONFIRMED | V2; BG Step 1 | This turn. |
| BO-02 | Postgres schema and migrations (DM-01). | CONFIRMED | V2; BG Step 2 | |
| BO-03 | Temporal worker skeleton, one hello workflow, Railway deploy, health endpoint. | CONFIRMED | V2 | Railway deploy needs PRE-07/08 (human). Everything else runs locally with mocks. |
| BO-04 | Graph integration: app registration, webhook + delta poll, draft and send as separate operations. | CONFIRMED | V2; BG Step 3 | |
| BO-05 | Model provider abstraction with per-agent routing and run logging. | CONFIRMED | V2 | |
| BO-06 | M1, A1, A2 with a mock data provider; real provider swapped in once selected. | CONFIRMED | V2 | |
| BO-07 | A3 InvestorSequence workflow; A4, A5; policy engine; review queue page; daily digest. | CONFIRMED | V2 | |
| BO-08 | Sheets sync; Zapier path behind flag. | CONFIRMED | V2 | |
| BO-09 | Synthetic pilot: 10 to 20 fake investors, test mailboxes. | CONFIRMED | V2; BG Step 11 | |
| BO-09b | Promote to live L0 (investor). | OPEN (human checkpoint) | V2 | Outside this build: USR forbids live messages. Requires your explicit go-live approval plus PRE items. |
| BO-10 | S1, S2, S3 SellerSequence; web form with photo upload; S4 intake. | CONFIRMED | V2 | |
| BO-11 | A7 with all six verification passes; S5; D1, D2. | CONFIRMED | V2 | |
| BO-12 | DocuSign templates; S6; deadline alerts. | CONFIRMED (mocked) | V2 | |
| BO-13 | Agent eval harness (golden test sets per agent); learning loop on outcomes. | CONFIRMED | V2 | |
| BO-14 | Seller synthetic pilot. | CONFIRMED | V2 | |
| BO-14b | Promote to live L0 (seller). | OPEN (human checkpoint) | V2 | Outside this build. |
| BO-15 | BG build order Steps 1 to 11 as the sequence. | REJECTED | BG §3 | Superseded by V2 Build order. Individual BG step contents survive above where not contradicted. |
| BO-16 | MS implementation phases 1 to 6. | REJECTED | MS §9 | Marked "proposed" in MS; superseded by V2 Build order. |
| BO-17 | BG Step 1 directory layout: /api, /workers, /agents, /integrations, /db, /tests, /infra, /docs. | PROPOSED | BG Step 1 | V2 only fixes `agents/llm.py`. Layout proposal in ENG-Q1. |
| BO-18 | Lint, type checking, unit tests, CI, dependency lockfile, architecture decision log. | CONFIRMED | BG Step 1; V2 | Tool choices ENG-Q2..Q4. |
| BO-19 | Acceptance for step 1: fresh checkout installs, tests run, no secrets committed. | CONFIRMED | BG Step 1 | |
| BO-20 | Definition of done: code, config templates, deployment infra, tests, runbook, operator dashboard; all agents pass end-to-end tests with synthetic data; live integrations verified; deployed worker survives restarts with costs and errors visible; user has approved sending permissions, outreach claims, jurisdictions, go-live checklist. | CONFIRMED | BG §7 | "Six investor functions and both deal-search functions" extended to all 16 V2 agents. |
| BO-21 | Daily KPI dashboard: discovered contacts, messages sent, responses, qualified buy boxes, matching properties, reviewed deals, contracts, assignment fees, cost per qualified buyer. Optimize on verified qualified responses and completed deals, not open rates. | CONFIRMED | BG Step 11 | Delivered as part of the daily digest / review queue page. |

**Build-time rules (USR, apply to every step of this build)**

| ID | Rule | Status | Source |
|---|---|---|---|
| RUL-01 | Do not assume any credentials, accounts, or paid services exist. Create `.env.example`, `SETUP.md`, and a human-action checkpoint for each; stop at each checkpoint. | CONFIRMED | USR; BG §0 |
| RUL-02 | Every integration behind an interface with a mock so tests and the synthetic pilot run without real services. | CONFIRMED | USR |
| RUL-03 | Autonomy policy table exactly as V2; same code path; default L0. | CONFIRMED | USR |
| RUL-04 | A7 with all six passes and the gate list as written; verification record persisted with every offer. | CONFIRMED | USR |
| RUL-05 | Every outbound send idempotent and logged; every agent run logged with prompt hash, model, tokens, cost, output. | CONFIRMED | USR |
| RUL-06 | No live messages, no purchases, no SMS, no contract or earnest-money automation at any point in this build. | CONFIRMED | USR |
| RUL-07 | Zapier parser behind `ZAPIER_PARSER_ENABLED`, default false. | CONFIRMED | USR |
| RUL-08 | After each milestone report: files implemented, tests passing, blocked items, exact next steps. | CONFIRMED | USR; BG §6 |
| RUL-09 | No 24/7 claim until Railway deployment is verified with a restart test. | CONFIRMED | USR |
| RUL-10 | Start with Build order steps 1 to 3. Ask before any architectural choice not covered by v2. | CONFIRMED | USR |
| RUL-11 | Use a multi-agent development workflow with separate owners for architecture, integrations, agent logic, QA/testing, deployment; integrate and review all contributions. | CONFIRMED | USR; BG §6 |
| RUL-12 | Label future items CONFIRMED / PROPOSED / REJECTED / OPEN; update this file and a change log when a requirement changes; never turn a proposal into a requirement silently. | CONFIRMED | MS §10 |

## 17. Pre-launch checklist (human actions, V2)

Each is a human-action checkpoint. I build the integration behind a mock and stop; you complete the item and provide the credential in `.env`.

| ID | Item | Status | Needed before |
|---|---|---|---|
| PRE-01 | Form LLC, get EIN. | OPEN | Seller L0 live |
| PRE-02 | Business physical address (CAN-SPAM, 10DLC). | OPEN | Any live send |
| PRE-03 | Business bank account. | OPEN | Seller L0 live |
| PRE-04 | Buy dedicated sending domain(s); SPF/DKIM/DMARC; begin warmup (start week 1). | OPEN | Investor L0 live |
| PRE-05 | Microsoft 365 business mailbox on the sending domain; confirm account type; Entra app registration. | OPEN | BO-04 live test |
| PRE-06 | Google account and Sheet for the operator view; service account. | OPEN | BO-08 live test |
| PRE-07 | Temporal Cloud account (namespace, mTLS cert or API key). | OPEN | BO-03 deploy |
| PRE-08 | Railway account and project. | OPEN | BO-03 deploy |
| PRE-09 | Model API key(s) (Anthropic by default). | OPEN | BO-05 live test |
| PRE-10 | Select and contract deed/transaction data provider. | OPEN | BO-06 real data |
| PRE-11 | Select and contract skip-trace provider. | OPEN | Seller phase |
| PRE-12 | Twilio account; 10DLC brand and campaign registration. | OPEN | Any SMS (never in this build) |
| PRE-13 | DocuSign account; PSA and assignment templates reviewed by a TX attorney. | OPEN | BO-12 live |
| PRE-14 | Title company per Active market that handles assignments. | OPEN | Seller L0 live |
| PRE-15 | Select and contract comps/AVM provider. | OPEN | Seller phase |
| PRE-16 | Approve sender identity, outreach templates (follow-ups, seller templates, offer template), send windows, daily caps. | OPEN | Investor L0 live |
| PRE-17 | Approve first Active markets from M1's initial run. | OPEN | Investor L0 live |
| PRE-18 | Confirm execution platform and permissions for always-on agents. | CONFIRMED | Resolved by V2: Temporal Cloud + Railway (MS §8 item closed). |

## 18. Open decisions with V2 defaults

The default applies until you decide. Each lives in versioned config, not code.

| ID | Decision | Default | Status | Source |
|---|---|---|---|---|
| OPN-01 | Assignment fee target per deal | $10,000 | OPEN | V2 |
| OPN-02 | market_pct for MAO | 0.70 | OPEN | V2 |
| OPN-03 | Negotiation room | 7% | OPEN | V2 |
| OPN-04 | Investor follow-up interval | 4 days | OPEN | V2; MS §2 |
| OPN-05 | Seller follow-up intervals | 3 / 7 / 14 days | OPEN | V2 |
| OPN-06 | Daily send caps at L1 | 50 investor, 200 seller email | OPEN | V2 |
| OPN-07 | Soft budget cap | $1,500/mo, alert at 80% | OPEN | V2 |
| OPN-08 | Markets to seed M1 with | Houston plus the 20 largest Sunbelt and Midwest metros | OPEN | V2 |
| OPN-09 | How often searches rerun and how property results are delivered to the operator | Rerun on buy-box change only (BuyBoxSearch) plus D2 on new listings; delivery via review queue page and daily digest | OPEN | MS §8; V2 |
| OPN-10 | QA confidence threshold for L1 auto-reply to interested investors | Not given in V2. Proposed 0.85 on A5's 0 to 1 confidence output | OPEN | V2 POL-03c |
| OPN-11 | 90-day results threshold that drops a market to Watch | Not given in V2. Proposed: <3 qualified buy boxes and 0 offers accepted in 90 days | OPEN | V2 M1 |
| OPN-12 | Repair per-sqft rate table per condition class per market; Houston foundation contingency amount | Not given in V2. Proposed placeholders Light $15, Medium $30, Heavy $50, Gut $75 per sqft; Houston foundation $12,000; all in config, flagged as unvalidated | OPEN | V2 A7 |
| OPN-13 | Closing cost fixed % table | Not given in V2. Proposed 3% of offer, per market override | OPEN | V2 A7 |
| OPN-14 | "Cash close in N days" in offer template | Not given in V2. Proposed 21 days | OPEN | V2 A7 |
| OPN-15 | Sending identity "[Your Name]" and company name in templates | None. Required before any live send | OPEN | MS §3; V2 |
| OPN-16 | Market hard cap (offer ≤ market hard cap gate) | Proposed: equals market deal band upper bound ($350k global) | OPEN | V2 A7 gates |

## 19. Consolidated v1 items superseded by v2

Each row already appears above; this list is the audit trail for "never silently replace a user decision."

| ID | v1 item | Source | Replaced by | Reason |
|---|---|---|---|---|
| V1R-01 | Google Sheets as the initial shared database | MS §1 | STK-02 | V2: Postgres is the only writer of truth; Sheets is a projection. |
| V1R-02 | Zapier for workflow orchestration | MS §1 | STK-03 | V2: Temporal. |
| V1R-03 | ChatGPT Dots as a candidate platform | MS §1, §7; BG §0 | STK-07 | V2: provider-agnostic model layer; runtime is Temporal/Railway. |
| V1R-04 | Redis + Celery / managed task queue | BG §1 | STK-03 | V2: Temporal. |
| V1R-05 | Buy-box path via parser → Zapier → Sheets → sync back to Postgres | BG §1 | INV-21 | V2: A4 → Postgres upsert → event; Zapier optional and off. |
| V1R-06 | Reconciliation from Sheets back to the database with version conflict handling | BG Step 4 | STK-02, WF-13 | V2: one-way Postgres → Sheets; agents never write Sheets; daily reconciliation only alerts. |
| V1R-07 | "Zapier or equivalent detects a new/updated buy-box row" | MS §5 Detect | BB-04 | V2: Postgres `buy_box.created/updated` event starts BuyBoxSearch. |
| V1R-08 | D1 listens to events "from the authoritative database and/or Sheets reconciliation" | BG Step 8 | BB-04 | Sheets is never a source of truth in V2. |
| V1R-09 | Draft-only pilot phase; operator approval "in pilot" | BG Step 3, 6, §6 | POL-01, POL-08 | V2: L0 is a per-action policy, not a phase. |
| V1R-10 | Generic cloud provider, chosen later | BG §1, §8, Step 10 | STK-04 | V2: Railway + Temporal Cloud. |
| V1R-11 | cron / cloud scheduler for recurring jobs | BG Step 10 | WF-01 | V2: Temporal schedules; nothing depends on cron. |
| V1R-12 | 07:00 local review schedule | BG §4 | WF-18 | Not in V2; BG marked illustrative. |
| V1R-13 | Market scoring proposal (7 qualitative factors) | MS §6 | M1-02/03 | V2: weighted numeric formula. |
| V1R-14 | BG Steps 1 to 11 order; MS phases 1 to 6 | BG §3; MS §9 | BO-01..14 | V2 Build order. |
| V1R-15 | Table names campaigns, scheduled_sends, consent_suppression, property_matches | BG Step 2 | DM-07..09 | V2 names: sequences, suppression, matches; Temporal holds send timers. |
| V1R-16 | "Keep ... the Zapier parser in the workflow" | BG §6 | INT-20 | V2: behind flag, default false. |
| V1R-17 | Five lead-source bots | MS §7 (already rejected) | AGT-X1 | User rejected; V2 S1 uses those signals only inside the M1-gated seller funnel. |
| V1R-18 | Sheets access via OAuth as an option | BG Step 4 | INT-15 | V2: service account. |
| V1R-19 | Scope "six investor functions and both deal-search functions" | BG §7 | AGT roster | V2: 16 agents including seller funnel. |

## 20. Engineering choices not covered by v2 — need your answer before I build steps 1 to 3

Per RUL-10 I am not deciding these alone. Each has my recommendation first. "Reply with the IDs you accept" is enough; anything you don't mention I will treat as accepted as recommended, because every one is reversible and none changes a product decision.

| ID | Choice | Recommendation | Alternatives | Status |
|---|---|---|---|---|
| ENG-Q1 | Repo layout | Single installable package `wholesaling/` with subpackages `api/`, `workers/`, `workflows/`, `activities/`, `agents/` (incl. `agents/llm.py`), `integrations/` (one subpackage per external service, each with `interface.py`, `mock.py`, `real.py`), `db/` (models, migrations), `policy/`, `config/`; top-level `tests/`, `infra/`, `docs/`, `scripts/`. Satisfies BG Step 1 folders inside one package. | BG's flat top-level folders as separate packages. | PROPOSED |
| ENG-Q2 | Dependency management | `uv` with `pyproject.toml` + `uv.lock`. | Poetry; pip-tools. | PROPOSED |
| ENG-Q3 | Lint / format / types | `ruff` (lint + format), `mypy` strict on `wholesaling/`. | black + flake8 + isort; pyright. | PROPOSED |
| ENG-Q4 | Tests and CI | `pytest` + `pytest-asyncio`; Temporal `WorkflowEnvironment` time-skipping test server for workflow tests; GitHub Actions CI with a Postgres service container; unit tests run against mocks with no services. | GitLab CI; Railway-only checks. Needs a Git host decision (ENG-Q12). | PROPOSED |
| ENG-Q5 | Database access and migrations | SQLAlchemy 2.x (async, `asyncpg`) + Alembic. Money as `NUMERIC(12,2)`; all timestamps `timestamptz` UTC; per-market IANA timezone column for local schedules. | Raw `asyncpg` + hand-written SQL migrations; Django ORM. | PROPOSED |
| ENG-Q6 | Config format for tunables (weights, rate tables, deal bands, caps) | YAML files under `config/` loaded with `pydantic-settings`, validated at startup, versioned in git; secrets only from env. `policies` stays a DB table per V2. | Everything in DB tables with an admin page; TOML. | PROPOSED |
| ENG-Q7 | Local development services | Docker Compose with Postgres 16 and Temporal dev server (`temporalio/auto-setup`) for local runs; tests do not require it. | Temporal CLI `temporal server start-dev` binary; Railway-only. Note this machine currently has no Docker (see section 22). | PROPOSED |
| ENG-Q8 | Default model IDs | Config keys `model.classify`, `model.extract`, `model.draft`, `model.qa`, `model.comp_audit`. Defaults: Anthropic Haiku for classify/extract, Sonnet for the rest, pinned to the current released IDs at implementation time and overridable per agent. Costs computed from a per-model price table in config. | Hard-code IDs in code. | PROPOSED |
| ENG-Q9 | Object storage for seller photos | Railway Volume for the pilot behind a `BlobStorage` interface; S3-compatible (Cloudflare R2) as the production implementation when you choose. Mock = local temp dir. | S3 from day one (needs an AWS account, not in V2's checklist). | PROPOSED |
| ENG-Q10 | A7 ARV weighting function | Weight per comp = `recency_weight × distance_weight` with linear decay: recency 1.0 at 0 days → 0.5 at the tier's day limit; distance 1.0 at 0 mi → 0.5 at the tier's mile limit. Trimmed set (15% each end by price/sqft) then weighted mean. Documented in config and the verification record. | Pure median; exponential decay. | PROPOSED |
| ENG-Q11 | A7 verification ordering | V1 after step 1 (facts), V3 then V2 after step 3 (ARV), V4 after step 4 (repairs), V5 after step 6 (offer), gates at step 7, QA at step 8, V6 immediately before step 9 (send) with gates re-run. All loops bounded as V2 states. This matches "between steps 6 and 7" for V5 and V2's own "immediately before send" for V6. | Run all six strictly between steps 6 and 7 (V6 would then not be "immediately before send"). | PROPOSED |
| ENG-Q12 | Git hosting and repo | Private GitHub repository (needed for GitHub Actions CI and Railway's GitHub deploy). You create it; I initialise locally and push on your go. | GitLab; Railway CLI deploy without a hosted repo. | OPEN |
| ENG-Q13 | Review queue and web form UI | Server-rendered FastAPI + Jinja2 + HTMX, no JS build step. | React SPA. | PROPOSED |
| ENG-Q14 | Multi-agent development workflow | Five sub-agents (architecture, integrations, agent logic, QA/testing, deployment) run in parallel per build step on disjoint files; I integrate and review every contribution before it lands. | Single-agent sequential build. | PROPOSED |

## 21. Checkpoint 0 — local toolchain (blocks running anything)

Discovered while reading the documents. This machine has: PowerShell 5.1, `winget`. It does not have: Python (only the Microsoft Store stub), Git, Docker, Node, Temporal CLI. I can write every file for steps 1 to 3, but I cannot install dependencies, run migrations, or run tests until these exist.

| ID | Item | Status | Notes |
|---|---|---|---|
| ENV-01 | Install Python 3.12 (`winget install Python.Python.3.12`). | OPEN (human action, or say "install it" and I will run winget) | Required for everything. |
| ENV-02 | Install Git (`winget install Git.Git`). | OPEN | Required for the repo, CI, Railway deploy. |
| ENV-03 | Install Docker Desktop, or accept the Temporal CLI dev-server alternative in ENG-Q7. | OPEN | Needed only for local Postgres/Temporal runs; unit tests run without it. |
| ENV-04 | Install `uv` (`winget install astral-sh.uv`). | OPEN | If ENG-Q2 accepted. |
| ENV-05 | Install Railway CLI and Temporal CLI. | OPEN | Needed at BO-03 deploy. |

## Change log

- 2026-10-25 — v1 of this file. Sources: MS v1.0, BG v1.0, V2 (docx + pdf). Nothing implemented yet.
