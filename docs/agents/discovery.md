# Discovery agents: M1, A1, A2 (build order BO-06)

Three agents turn provider data into scored markets and scored investors with contact channels.
All three are deterministic code over provider records (V2 "Haiku + code": the scoring is the
code half; any later model use is additive and optional). None of them imports the model layer.

| Agent | Module | Input | Output | Writes |
|---|---|---|---|---|
| M1 Market Selection | `wholesaling/agents/m1.py` | `MarketSignals` per seed market | `M1Report` (ranked, per-signal contributions, gate results, tier decisions) | `markets` |
| A1 Investor Discovery | `wholesaling/agents/a1.py` | `DeedRecord`s for one market, 24-month window | `A1Result` (counts) | `investors`, `source_evidence` |
| A2 Contact Research | `wholesaling/agents/a2.py` | `ContactCandidate`s for one investor | primary email or `None` (`A2Result` for detail) | `contacts` |

Pure rules live in `wholesaling/agents/scoring.py` (no I/O, no clock); entity grouping in
`wholesaling/agents/entity_resolution.py`; the Temporal wrappers in
`wholesaling/activities/discovery.py` (`run_m1`, `run_a1`, `run_a2`).

## Data providers (`wholesaling/integrations/data/`)

| Protocol | Method | Used by |
|---|---|---|
| `DeedRecordsProvider` | `purchases(market, since, until) -> list[DeedRecord]` | A1 (M1, S1 later) |
| `MarketSignalsProvider` | `signals(market) -> MarketSignals` | M1 |
| `ContactEnrichmentProvider` | `lookup(entity_name, mailing_address, registered_agent?) -> list[ContactCandidate]` | A2 |

`get_data_providers(settings)` returns a `DataProviders` bundle (the three providers plus
`ReferenceLists`: known institutional entities, licensed agents, suppressed entity keys) keyed by
`DATA_PROVIDER_MODE`. Only `mock` exists; `live` raises `NotImplementedError` because INT-03 is
OPEN.

### Swapping in a real provider (INT-02)

1. Score candidates (county APIs, ATTOM, PropStream, BatchData) on INT-02's four criteria:
   coverage in the Active/Watch markets, API access, cost per record, licence permits automated
   use. INT-07: confirm terms before assuming search access exists.
2. Add `wholesaling/integrations/data/real_<vendor>.py` implementing the Protocol(s) above. Map
   the vendor's fields onto `DeedRecord`; set `financing="unknown"` when no mortgage search was
   possible (A1 treats unknown as not cash), and return resales *by* investors as well as
   purchases so A1 can see flip and wholesale patterns.
3. Construct it in `get_data_providers` under `settings.data_provider_mode == "live"`; add the
   vendor's secrets to `Settings` and `.env.example` (the parity test enforces both).
4. Load `ReferenceLists` from a versioned config file instead of the fixture.
5. Run A1 on the mock and the live provider for the same market and compare `A1Result` counts
   before trusting the live numbers.

### Mock fixtures

`wholesaling/integrations/data/fixtures/*.json` are generated and committed. Regenerate with

```
uv run python -m wholesaling.integrations.data.generate_fixtures
```

The generator is deterministic (seed `20261025`, anchor `AS_OF = 2026-10-01T00:00Z`); the test
`test_committed_fixtures_match_the_generator` fails if the files drift. Houston (47 deeds) is
hand-designed so every rule has a named scenario; the other 20 seed metros are seeded-RNG volume
(Birmingham, AL deliberately has `data_available=False` to exercise the M1 data gate).

| Scenario | Rule | Expected |
|---|---|---|
| Bayou Capital Partners (3 LLC spellings, 4 cash buys, 2 flips held 7-8 mo) | INV-06, INV-03 Flipper | one investor, 6 evidence rows, score 89, priority |
| Gulf Coast Rentals LLC + Gulf Coast Rentals II LLC at one PO box, 5 buys in ZIP 77021 | INV-06 shared address + fuzzy name, INV-03 Buy-and-hold | one investor, score 89, priority |
| Maria Hernandez, 2 cash buys in 24 mo | INV-02 individual >=2 | score 67, prospect |
| John Carter, 1 buy | INV-02 | not qualified: `individual_below_two_purchases` |
| Opendoor Property Trust I | INV-03/05 known entity list | excluded `institutional` |
| Lennar Homes of Texas LLC, 3 lots | INV-05 builders buying lots | excluded `builder_lots` |
| Fast Close Houston LLC, 3 buys, 2 resold within a month | INV-03 Wholesaler | score 72 (-5 bonus), prospect |
| Westside Living LLC, 3 conventional mortgages | INV-02 cash/hard money | not qualified: `no_cash_purchase` |
| Luxury Estates Group LLC, $900k and $1.2M cash | INV-02 / M1-06 band | not qualified: `outside_deal_band` |
| Old Money Investments LLC, last buy 20 mo ago | INV-02 <=18 months | not qualified: `stale_last_purchase` |
| Harris County / Habitat for Humanity Houston Inc / Wells Fargo Bank NA | INV-05 | excluded `government` / `nonprofit` / `lender_reo` |
| Suppressed Holdings LLC | INV-05 suppression | excluded `suppressed` |
| Lone Star Flips LLC, 2 hard-money buys, 1 flip | INV-02 hard money, INV-03 | score 76, flipper, priority |
| Keller Realty Investments LLC (licence list) | INV-03 Agent-investor | score 72, `agent_investor`, included |
| Heights Holdings LLC, 2 of 3 in band, same registered agent as Fast Close | INV-04 band fit, INV-06 no merge on address alone | score 72, separate row |

A1 on Houston with the global band: deeds 47, found 22, qualified 7, prospects (>=50) 7,
priority (>=75) 3, 13 investor rows written (7 qualified + 6 exclusions), 38 evidence rows;
excluded by reason: builder_lots 1, government 1, individual_below_two_purchases 6 (John Carter +
5 retail buyers on resale deeds), institutional 1, lender_reo 1, no_cash_purchase 1, nonprofit 1,
outside_deal_band 1, stale_last_purchase 1, suppressed 1. Rerunning changes nothing.

## M1 scoring (REQUIREMENTS.md section 6)

`market_score(signals, weights, friction)` implements M1-03 after the M1-02 data gate:

| Signal | Weight (config/m1.yaml) | Component (0..1) | Row |
|---|---|---|---|
| Investor demand | 0.30 | mean of entity share / 0.35, cash share / 0.50, repeat buyers / 200, i2i resale share / 0.15 | M1-02 |
| Spread | 0.25 | cash-vs-retail spread / 0.30, averaged with median flip profit / $60k when present | M1-02 |
| Distress supply | 0.20 | (pre-foreclosure + tax + probate + absentee + vacancy per 1,000) / 40 | M1-02 |
| Liquidity | 0.10 | mean of 1 - DOM/90, 1 - months inventory/6, (sold trend + 0.2)/0.4 | M1-02 |
| Price band fit | 0.10 | share of stock inside the deal band | M1-02 |
| Legal friction | -0.05 | state score 0..3 divided by 3 | M1-02 |
| Data availability | gate | `data_available=False` -> Excluded, no score | M1-02, M1-04 |

The normalisation scales (`SignalScales`) are not in V2: they are documented defaults to be
retuned from realized results (M1-03). Friction 3 fails the legal gate. A state with **no**
friction entry scores 0 but is flagged `legal_review_pending` and cannot be promoted to Active
(config/m1.yaml: every entry needs the CMP-04 legal review). Only TX has an entry today.

### Tiers (M1-04, M1-07)

`tier_markets(...)`:

- Gate failure -> `Excluded` immediately (facts, not thresholds).
- New markets start in `Watch`. Nothing is `Active` on the first run; approving the first Active
  markets is the human checkpoint M1-11, taken from the `M1Report`.
- Proposed `Active` = rank within `active_cap` (5) **and** >= `watch_to_active_min_buy_boxes`
  (10) current, active buy boxes held by investors in that market **and** legal review confirmed.
- A Watch<->Active change applies only when proposed on two consecutive runs
  (`tier_crossings` in `markets.signals`, reset when proposal and tier agree again).
- Excluded -> gates pass -> `Watch` immediately (re-entry, not promotion).

### Deal band (M1-06)

`deal_band_for_market(median, global)`: median x 0.5 .. median x 1.5 clamped into the global
$75k..$350k purchase band; no median -> global band. Written to `markets.deal_band_min/max` and
used by A1 for that market. Houston's fixture median $262k gives $131k..$350k.

## A1 rules (REQUIREMENTS.md section 7)

Window: 24 months ending at `as_of` (INV-02 "in 24 months"). Candidates come from
`resolve_entities` (INV-06): exact normalised entity key always merges; different keys merge only
on exact normalised mailing address **plus** Jaccard >= 0.6 on suffix-stripped name tokens;
individuals never merge across keys; every deed stays as `source_evidence`.

Order per candidate: `is_excluded` (INV-05) -> `qualifies` (INV-02) -> `classify_investor_type`
(INV-03) -> `investor_score` (INV-04) -> write.

| INV-02 rule | Check | Failure reason |
|---|---|---|
| entity, or individual with >=2 purchases in 24 months | `is_entity or len >= 2` | `individual_below_two_purchases` |
| cash or hard money, no conventional mortgage | `financing in {cash, hard_money}` (unknown is not cash) | `no_cash_purchase` |
| residential 1-4 unit | `property_type in {sfr, duplex, triplex, fourplex}` and 1-4 units | `not_residential_1_4` |
| price inside the market deal band | inclusive | `outside_deal_band` |
| last purchase <= 18 months | any purchase | `stale_last_purchase` |

| INV-03 type | Rule | Treatment |
|---|---|---|
| iBuyer / institutional | known entity list | Exclude (never scored) |
| Agent-investor | licence list | Flag, include (bonus 0) |
| Wholesaler | >=2 resales held < 4 months and >= 50% of purchases | Deprioritize (-5) |
| Flipper | any resale held 4-12 months at a higher price | Priority (+5) |
| Buy-and-hold | majority held > 12 months, or >=50% in one ZIP with a long hold and no resales | Priority (+5) |
| Unknown | otherwise | 0 |

| INV-04 component | Points | Formula |
|---|---|---|
| Volume | 30 | 30 x min(purchases in 24 mo, 6) / 6 |
| Recency | 25 | 25 x (1 - months since last / 18), floored at 0 |
| Cash rate | 15 | 15 x share of purchases that are cash or hard money |
| Price-band fit | 15 | 15 x share of purchases inside the band |
| Market concentration | 10 | 10 x share of the investor's purchases inside this market |
| Type bonus | +-5 | table above |

>= 50 prospect (A2 runs), >= 75 priority (first in the outreach queue). The saturation point of
6 purchases and the wholesaler/cluster shares are documented defaults, not V2 numbers.

| INV-05 exclusion | Rule |
|---|---|
| suppression | entity key in the suppression set (reference list or runtime argument) |
| institutional | entity key in the known list |
| government | tokens COUNTY / HUD / AUTHORITY, or phrases "CITY OF", "STATE OF", "HOUSING AUTHORITY", ... |
| nonprofit | phrases "HABITAT FOR HUMANITY", "FOUNDATION", "CHURCH", "MINISTRIES", ... |
| lender taking REO | tokens BANK / MORTGAGE / LENDING / FSB / BANCORP, phrases "CREDIT UNION", "FANNIE MAE", ... |
| builder buying lots | tokens HOMES / BUILDERS / CONSTRUCTION / DEVELOPMENT **and** >= 50% of purchases are land |

Name rules match whole tokens of the normalised key, never substrings.

Write policy: qualified candidates get score, type, breakdown, mailing address, aliases and
evidence; INV-05 exclusions are written with `excluded_reason` and evidence but no score; INV-02
failures are counted and not written.

## A2 (INV-08, INV-09)

Lookup by canonical name and mailing address; normalise with `normalize_email` /
`normalize_phone` / `normalize_address`; junk (unparseable, "n/a") dropped; duplicates folded per
`(channel, normalized_value)` keeping the highest confidence; emails and phones on the
`suppression` table skipped; a value already attached to another investor is logged and left
there (unique across investors, DM-02). Rows carry `provenance` (`source: detail`),
`provenance_url`, `use_limitations`, `confidence`. The highest-confidence email is `is_primary`
and returned. Phones are stored with an explicit "capture only" limitation: the investor channel
is Outlook only, no calls or SMS.

## Activities and the discovery lock (WF-08, WF-09)

- `run_m1(as_of?)` -> `M1Report.to_json()`; schedule monthly.
- `run_a1(market_id, as_of?)` -> `A1Result.to_json()`; schedule daily 06:00 market-local. It
  takes the per-market lock first (`markets.signals["a1_running_since"]`, committed in its own
  transaction); a held lock returns `{"skipped": "locked"}` instead of failing; a lock older than
  6 hours is treated as a crashed run and taken over; the lock is released in `finally`.
- `run_a2(investor_id)` -> `A2Result.to_json()`.

Tests inject dependencies with `activities.discovery.configure(session_factory, providers)`.

## Decisions not covered by the spec

1. Signal normalisation scales, volume saturation (6), wholesaler fast-resale share (50%, >= 2),
   ZIP cluster share (50%), builder lot share (50%), name-similarity threshold (0.6).
2. Markets with no legal-friction entry stay eligible for Watch but not Active.
3. `market_concentration` = share of the investor's purchases inside the target market (1.0 when
   the provider only returns in-market deeds).
4. Excluded entities are written as investor rows with `excluded_reason`; INV-02 failures are not.
5. Qualified buy boxes per market = current, active `buy_boxes` rows joined through
   `investors.market_id`.
6. `DATA_PROVIDER_MODE` added to settings and `.env.example`.
7. The mock providers' fixture generator lives at
   `wholesaling/integrations/data/generate_fixtures.py` so the fixtures can be rebuilt.
