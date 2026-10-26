# D1 search profiles and D2 matching (build order BO-11)

REQUIREMENTS.md section 9 (BB-04..BB-12), WF-07, WF-20, OPN-09. Code: `wholesaling/agents/d1.py`,
`wholesaling/agents/d2.py`, `wholesaling/activities/deals.py`,
`wholesaling/workflows/buy_box_search.py`, the `_run_search_steps` step of
`wholesaling/workflows/buy_box_event.py`, `wholesaling/db/repositories/deals.py`.

## Event path (WF-20, BB-04..BB-06)

```
buy_box.created|updated (audit, A4)  ->  BuyBoxEventWorkflow (buy-box-event-{id}-v{n})
    step 2  run_d1  ->  search_profiles: create | coalesce | supersede
                        created  -> start BuyBoxSearch (buy-box-search:{buy_box_id}:v{profile_version})
                                    + signal `superseded` to the previous version's workflow
                        coalesced-> nothing queued (same criteria hash)
    step 3  A6 parser (unchanged)
BuyBoxSearch  ->  run_d2 once  ->  property_candidates + matches (review_status=pending)
```

BB-06 acceptance: one new buy box -> exactly one active `search_profiles` row and exactly one
`BuyBoxSearch` start (`tests/activities/test_deals.py::test_run_d1_creates_one_profile_and_queues_exactly_one_search`).

## D1 normalisation (BB-04)

| field | normalised to |
|---|---|
| geography | `markets` as `City, ST`, `cities` title-cased, 5-digit `zips`, `neighborhoods` (all sorted, deduped) |
| asset types | `property_classes`: `sfr`, `small_multi` (duplex .. fourplex, 2-4 units), `condo_townhome`, `land`, `commercial`; anything else kept as a slug |
| price range | `price_min` / `price_max` as whole-dollar strings |
| condition | `condition_tolerance`: worst class accepted, `light` < `medium` < `heavy` < `gut` ("any" = gut) |
| strategy / financing / closing / other criteria | lower-cased, slug keys, carried through |
| unknowns | A4's `unknown_fields` + missing geography / property classes / price_max / condition |
| wording | `original_wording` = the investor's raw text, kept on the profile but **excluded from the hash** |

`criteria_hash` = SHA-256 of the canonical JSON minus wording keys. Same hash as the active
profile -> coalesce (no new version, no search, audit `search_profile.coalesced`); different ->
new version, previous row `superseded`, audit `search_profile.created` + `search.queued`.

## D2 matching (BB-07..BB-09, BB-12)

Sources: the property provider's active listings inside the profile's geography, plus
off-market candidates = A7 offers with status `accepted` (BB-12) carrying A7's verified ARV and
repairs. Candidates are deduped by parcel id, then by the normalised address key (`dedupe`
reports what collapsed); `property_candidates` is unique on `(source, external_id)` and the
repository also collapses cross-source duplicates by parcel / address, remembering the other
source id in `raw.also_seen`.

Per candidate, **separate estimates** with A7's own functions: ARV (`a7.arv` over the ZIP comp
pool with tier expansion), repairs (`a7.repairs` from the listing's condition hint, Medium when
unknown), fees (assignment fee from config), assignment spread (MAO - list price), liquidity
proxy (tier-1 comps / 10, capped at 1).

Hard filters (no match row): outside geography; property class not in the profile; list price
more than 25% outside the range; condition worse than the tolerance when both are known. Score
= 0.20 geography (ZIP 1.0, city 0.8, market 0.6) + 0.15 property class + 0.25 price fit (1.0 in
range, linear to 0 at the 25% slack) + 0.15 condition + 0.25 spread (spread / fee, capped at 1).
Confidence = data completeness (1 - missing fields / 10) x ARV-confidence factor (High 1.0,
Medium 0.75, Low / none 0.40).

Flags for human due diligence: `hoa`, `title_liens`, `financing_constraint:<terms>`,
`multi_unit`, `non_residential`, `data_gap:<field>`, `no_arv_estimate`.

Every match's `evidence` (BB-08) holds: `source`, `source_url`, `external_id`, `retrieved_at`,
`freshness` (days since retrieval, days on market), `match_logic` (weights, components, the
profile criteria used), `estimates`, `missing_fields`, `flags`, `source_restrictions`,
`valuation_uncertainty` (ARV confidence, spread, comp count / tier, repair basis), `confidence`,
`seller_intent`, `reason`, `next_review_action`, `review_status` (always `pending` on write, BB-09),
`rank`, `profile_version`. Every text passes `templates.check_no_agreement_claim`: a listing's
seller intent is "unknown: no contact made"; an off-market candidate says "A7 offer status
accepted; purchase contract still requires a human signature (SEL-11)". A re-run refreshes score
and evidence but never touches a human's review status.

## BuyBoxSearch (WF-07)

No sleeps. `superseded(SupersededSignal(new_version, new_search_profile_id)|None)` before the
run skips the search; during or after it is recorded (`superseded_by_version`). `run_d2` also
refuses a profile that is not `active`, so a late start cannot write matches for a stale version.

## Decisions the spec did not cover

- Profiles are keyed on the `buy_boxes` row id; a corrected buy box that produces a new version
  row gets its own profile lineage (`v1`) and search, while an in-place change to the same row
  supersedes (`v2`). Both paths queue exactly one search.
- A listing without a list price scores 0.5 on price fit and 0.3 on spread rather than being
  excluded; the gaps are flagged.
- Off-market candidates use the accepted price as the list price and A7's ARV / repairs.
- The 25% price slack, the component weights and the liquidity proxy are D2 constants
  (`d2.WEIGHTS`, `d2.PRICE_SLACK`); they are candidates for `config/` once the operator has
  reviewed a few hundred matches.
