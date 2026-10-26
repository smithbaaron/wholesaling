# config/

Versioned, non-secret tunables. Loaded and validated by `wholesaling/config/tunables.py` at startup.
Secrets never go here; they go in `.env` (see `.env.example`).

| File | Holds | REQUIREMENTS.md |
|---|---|---|
| `deal_band.yaml` | Global purchase / ARV band | M1-06 |
| `m1.yaml` | M1 weights, caps, legal-friction scores per state, seed markets | M1-02, M1-03, OPN-08 |
| `sequences.yaml` | Follow-up intervals, negotiation cadence | OPN-04, OPN-05, SEL-09 |
| `a7.yaml` | Offer engine constants, repair rate table, comp tiers | A7-03..07, OPN-01..03, OPN-12..14 |
| `caps.yaml` | L1 daily send caps, QA confidence threshold, optional `outreach_kickoff_daily_cap` (WF-02 starter; defaults to `OUTREACH_KICKOFF_DAILY_CAP`) | OPN-06, OPN-10, WF-02 |

Every value marked OPEN in REQUIREMENTS.md section 18 is a default here. Change the YAML, not the code,
and record the decision in REQUIREMENTS.md's change log.
