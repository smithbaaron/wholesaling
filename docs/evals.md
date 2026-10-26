# Agent eval harness and learning loop (build step BO-13)

REQUIREMENTS.md BO-13 ("agent eval harness (golden test sets per agent); learning loop on
outcomes"), INV-22 (sarcasm, corrections, multiple buy boxes, quoted text, opt-outs,
out-of-office in the A4/A5 tests), INV-11 / INV-19 (subject-variant and response-rate
tracking), M1-05 (per-market realized results), BO-21 (optimise on qualified responses and
completed deals). Code: `wholesaling/evals/`, `wholesaling/reporting/learning.py`,
`wholesaling/activities/learning.py`, `wholesaling/workflows/learning_stats.py`; golden sets in
`tests/agents/golden/`; tests in `tests/evals/` and `tests/reporting/test_learning.py`.

The harness never changes an agent. A golden case an agent fails is a **finding** in the report
(and, once it is in `baseline.json`, a known failure rather than a regression).

## What is covered

| Agent | Task | Golden file | Cases | What the runner drives |
|---|---|---|---|---|
| A4 | `classify` | `inbound_replies.json` | 29 | quote stripping, auto-reply short-cut (no model call), `is_auto_reply` forcing `ambiguous`, reconnect-date parsing, prompt content (`stripped_text_contains` / `_excludes`), Sonnet routing override |
| A4 | `extract` | `inbound_replies.json` (`extraction` blocks) | 5 | buy-box keys, stated criteria, unknown tracking, correction history against the current version, never-invent |
| A5 | `qa` | `a5_qa.json` | 26 | the five deterministic pre-checks (prohibited claims, opt-out, wrong recipient, repeated ask, 24 h cadence) that must block without a model call, the `scheduled_sequence` waiver, model path (approve / rewrite / block), `sensitive_content` and `rewrite_without_body` flags |
| S4 | `intake` | `seller_intake.json` | 18 | classification (auto-reply short-cut, `asks_offer`), extraction into `IntakeFields`, merge into the intake row, `missing_required`, `seller_unsure` not re-asked, `*_unknown` flags, never-invent |
| A7 | `comp_audit` (V3) | `a7_comp_audit.json` | 17 | deterministic pre-filters per comp (new build, lot-value outlier, renovated flip, non-arm's-length by surname / remarks / price), model flags merged on top, "model never removes a pre-filter flag", empty set = no model call |

95 cases, all passing in scripted mode (`baseline.json`). A4 `draft` is in the schema's task
table but has no committed cases yet (the draft post-processing is covered by
`tests/agents/test_a4.py`); add `a4_draft.json` with `task: "draft"` when draft quality becomes a
question worth a golden set.

## The golden-case schema

Every case, whatever file it lives in, is loaded into one shape (`wholesaling/evals/cases.py`):

```json
{
  "id": "prohibited_inventory",
  "agent": "A5",
  "task": "qa",
  "input": { "...": "what the agent receives" },
  "expected": { "...": "what the runner checks" },
  "tags": ["prohibited_claim", "precheck"],
  "notes": "XC-07 'inventory' denylist",
  "model": { "...": "optional: the structured output the scripted model returns" }
}
```

A file is `{"_meta": {...}, "cases": [...]}`. Three on-disk formats are accepted and the loader
detects them (`_meta.format`, else by shape):

- **unified** - the shape above (`a5_qa.json`, `a7_comp_audit.json`). Use it for new files.
- **inbound_replies** - the A4 set as BO-07 wrote it. Each entry becomes an `A4/classify` case
  and, when it has an `extraction` block, a second `A4/extract` case with id `<id>:extract`.
- **seller_intake** - the S4 set as BO-10 wrote it. Each entry becomes an `S4/intake` case.

The shims read the files as they are; `tests/agents/test_a4.py` and `test_s4.py` keep driving
them directly, and `tests/evals/test_cases.py` asserts the shim preserves every `expected`
value of every entry. Validation (`validate_case`): id, known agent, a task that agent has,
non-empty `input` and `expected`, `tags` a list of strings, unique `(agent, id)` across all files.
A schema error fails `wholesaling-evals` with exit 2 and fails `tests/evals/test_cases.py`.

### Per-task `input` and `expected`

**A4 classify / extract** (`inbound_replies.json` entry): `subject`, `body` (raw, quoted history
included where the case tests it), `headers`, `history` (`[{direction in|out, body}]`), `model`.
`expected`: `classification`, `is_auto_reply`, `quoted_text_stripped`, `reconnect_date`,
`stripped_text_contains` / `stripped_text_excludes` (checked on the prompt actually sent),
`model_called`. `extraction`: `{current: {key: criteria}, model, expected: {buy_boxes: [...]}}`
where each expected box may carry `buy_box_key`, `price_min`, `price_max`, `asset_types`,
`markets` / `cities` / `zips` / `neighborhoods`, `condition`, `strategy`, `financing`,
`closing_timeline`, `unknowns`, `corrections`. List every field the investor stated: the
harness synthesises the model output from `expected` in `--script-from expected` mode, so an
incomplete `expected` under-states the extraction and fails `unknowns`.

**A5 qa** (`a5_qa.json`): `input` = `draft {to, subject, body, asks_for, contains_sensitive}`,
`thread [{direction, body, hours_ago?, from?}]`, `facts`, `current_buy_box`,
`thread_participant`, `recipient_suppressed`, `last_outbound_asks_for`,
`last_outbound_hours_ago`, `scheduled_sequence`. `expected` = `verdict`, `flags` (exact final
set) and/or `flags_include` (required subset), `model_called`, `model_flags` (what the scripted
model adds), `min_confidence`, `has_rewrite_body`, `rewrite_body` (scripted),
`rewrite_body_excludes`. Pre-check cases must say `model_called: false`.

**S4 intake** (`seller_intake.json` entry): `subject`, `from`, `body`, `headers`, `history`,
`known {parcel_verified}`, `model {classify, extract}`. `expected`: `classification`,
`is_auto_reply`, `asks_offer`, `quoted_text_stripped`, `model_called`, `extract_called`,
`fields` (every stated `IntakeFields` value; `asking_price` as a decimal string),
`missing_required`, `seller_unsure`. Fields not listed must come back null (never-invent).

**A7 comp_audit** (`a7_comp_audit.json`): `input` = `subject` (address, sqft ...), `as_of`,
`comps` (`SoldComp.to_json()` documents). `expected` = `prefilter {comp_id: [flags]}` (what the
deterministic layer must flag), `flagged {comp_id: [flags]}` (final flags; absent comps are
clean), `model_flags {comp_id: [flags]}` (what the scripted model adds). Flags are
`non_arms_length`, `new_build`, `renovated_flip`, `lot_value_outlier`.

## Adding a case

1. Pick the file for the agent (or start a new unified file in `tests/agents/golden/`; the
   loader picks up every `*.json` there). Write `input` as the pipeline would hand it over and
   `expected` as the *behaviour the requirements want*, not what the agent happens to do.
2. For a deterministic path (pre-check, auto-reply, pre-filter) say `model_called: false`; the
   runner asserts no request reached the provider and confidence is 1.0.
3. Leave `model` out unless the scripted response must differ from the perfect one (for
   example `auto_reply_flagged_by_model`, where the model says `interested` but flags
   `is_auto_reply`). Without a `model` block the runner synthesises the structured output from
   `expected` (`runners.model_output_from_expected`); `tests/evals/test_runners.py` checks every
   synthesised output against the agent's JSON schema.
4. Run `uv run wholesaling-evals run --agent <A>`. A failing case is either a wrong expectation
   (fix the case) or an agent defect (leave the case, record the finding below, and either fix
   the agent in its own build step or accept it into the baseline with
   `--update-baseline`, which lists it under `failing_ids`).
5. Run `uv run wholesaling-evals run --update-baseline` so `baseline.json` knows the new total
   (`tests/evals/test_regression_gate.py::test_baseline_covers_every_committed_case` fails until
   you do), and commit the golden file and the baseline together.

Existing values in `inbound_replies.json` and `seller_intake.json` are load-bearing for
`tests/agents/test_a4.py` / `test_s4.py`: add fields and cases, never change an expected value
without changing those tests.

## Running

```bash
uv run wholesaling-evals run                              # every agent, scripted, gated on baseline.json
uv run wholesaling-evals run --agent A4 --agent A5        # a subset (the gate compares those agents only)
uv run wholesaling-evals run --script-from expected       # ignore `model` blocks: synthesise from expected
uv run wholesaling-evals run --out report.md --json report.json --verbose
uv run wholesaling-evals run --tag prohibited_claim --no-gate
uv run wholesaling-evals list --agent A7
```

Exit status: 0 no regression, 1 regression (new failing case or an agent's pass rate below its
baseline), 2 usage / schema error or live provider unavailable.

### Scripted mode (`--provider mock`, default)

A fresh `MockModelProvider` per case, scripted per task kind from the case (`model` block, else
synthesised from `expected`). This validates every deterministic step around the model with
production code: `A4ReplyAgent.classify / extract_buy_box`, `A5QaAgent.review`,
`S4IntakeAgent.classify / extract` plus `SellerRepository.merge_intake` and
`missing_required`, `A7CompAuditAgent.audit` with `prefilter_flags`. Routing uses the production
overrides (A4 classify and A7 comp audit on Sonnet). Each case runs in its own SQLite session
that is rolled back, so `agent_runs` rows are written like in production and discarded.

### Live mode (`--provider anthropic`)

Once `ANTHROPIC_API_KEY` exists in the environment (or `.env`):

```bash
ANTHROPIC_API_KEY=... uv run wholesaling-evals run --provider anthropic --no-gate --out live.md --json live.json
```

The same cases are sent to the real models through `AnthropicProvider` (retries, breaker,
`agent_runs` logging as in production). The committed baseline is scripted-mode data, so run
live with `--no-gate` (or point `--baseline` at a live baseline you keep separately; the CLI
refuses to write a live baseline over the scripted one). Without a key the CLI prints
`skipped: ANTHROPIC_API_KEY is not set` and exits 2; the pytest live test is skipped unless
both `ANTHROPIC_API_KEY` and `EVALS_LIVE=1` are set (it spends money).

What to expect live, by construction of the checks: A4 classify compares the class, auto-reply
flag and reconnect date; `stripped_text_*` still checks the prompt the harness sent. A4 extract
compares stated values (set-F1 for lists, exact for numbers) and the unknowns list. A5 pre-check
cases cannot differ live (the model is never reached); model-path cases compare the verdict and
the deterministic flags (`sensitive_content`, `rewrite_without_body`) exactly and model flags
only where a case uses `flags_include`, because model flag names are free text. A7 compares the
final flag set per comp, so a live model that flags *more* than the pre-filters fails the case
and shows up as a finding to read, not a defect by definition.

### The baseline gate

`wholesaling/evals/baseline.json` holds, per agent, `total`, `passed`, `pass_rate` and
`failing_ids` from the last `--update-baseline` run in scripted mode. `compare_to_baseline`
fails when an agent's pass rate drops below its baseline or a case not in `failing_ids` fails
("new failure", printed per id). Cases already in `failing_ids` are findings, not regressions;
when one starts passing the summary says so, and the next `--update-baseline` drops it.

CI runs pytest, and `tests/evals/test_regression_gate.py` runs the full scripted harness and
asserts the gate, so no separate CI step exists. `test_baseline_covers_every_committed_case`
keeps the baseline totals equal to the committed case counts.

## Learning loop on outcomes (`reporting/learning.py`)

Pure functions over outcome projections; nothing here writes.

- `attribute_replies(touches, replies)` - a reply belongs to the latest outbound touch on its
  thread sent before it arrived; replies with no earlier touch are `unattributed`. Automatic
  replies (`messages.meta.a4.is_auto_reply`) and `unrelated` never count as responses.
- `template_stats(outcomes)` -> `TemplateStats` per `(template_key, subject_variant, kind)`:
  `sent`, `replied`, `qualified_replies` (interested / buy_box_supplied), `opt_outs`, and the
  three rates (INV-11 subject experiments judged on qualified response rate, INV-19 response and
  opt-out tracking; no open rate exists, BO-21). `template_key` and `subject_variant` are read
  from the outbound row's `meta` written by A3 (`outreach.prepare_step`) and S3.
- `market_results(outcomes, thresholds=None)` -> `MarketResults` per market: touches, replies,
  qualified replies, opt-outs, buy boxes captured (current + active versions only), buy-box
  versions, offers made / accepted, assignments closed (`contracts.status = closed`), realized
  fees. `below_threshold` is `None` until a `MarketThresholds` is supplied (M1-05's value is
  OPEN, OPN-11); with one it is advisory with `threshold_reasons`. **No market tier is changed
  here**; M1 consumes the event when the threshold decision lands.
- `load_outcomes(session, start, end)` reads the window: outbound rows sent in it, inbound rows
  received in it (plus later replies on those threads up to the window end), buy boxes by
  `created_at`, offers by `updated_at`, contracts by `closed_at` (or `updated_at` while open).
  Market comes from `investors.market_id` / `seller_leads.market_id`, falling back to the
  outreach row's `meta.market`.

`activities/learning.py::compute_learning_stats(LearningInput(day, window_days=90))` loads the
trailing window ending at the start of the day after `day` (operator timezone), computes
`LearningStats`, and writes one `learning.stats` audit event (payload = `LearningStats.as_dict()`
plus a `summary`), idempotent per (day, workflow id). `LearningStatsWorkflow` wraps it and the
`learning-stats` schedule fires it daily (`workflows/schedules.py`); both are registered in the
worker. The digest and `/policies` do not read the event yet; that is the consumer side of the
loop for the next reporting step.

## Findings from the first run

Scripted mode, 95/95 pass for both `--script-from case` and `--script-from expected`. No agent
behaviour fails a golden expectation. Observations recorded while building the sets:

1. **A4 extraction expectations were incomplete, not wrong.** `buy_box_single`,
   `buy_box_two_in_one_email` and `buy_box_partial_tracks_unknowns` listed only part of what the
   investor stated (no `markets` / `cities` / `neighborhoods` / `condition` / `strategy` /
   `closing_timeline`), so synthesising the model output from `expected` under-stated the box
   and the agent correctly reported the missing fields as unknown. The missing fields were
   **added** to `extraction.expected` (file `_meta.version` 2); every previously asserted value
   is unchanged and `tests/agents/test_a4.py` still passes. Lesson folded into "Adding a case".
2. **A5 pre-check flags accumulate; the model is never consulted on a dirty draft.** A draft
   that is both prohibited and to a suppressed address reports both flags (case
   `opt_out_plus_prohibited_claim`); the cadence boundary is strict (`< 24 h` blocks, exactly
   24 h passes, case `cadence_exactly_24h_ok`). Both are the intended INV-17 behaviour and now
   pinned.
3. **A5 cannot check the recipient without a thread participant.** With
   `thread_participant=None` a draft to any address passes the pre-checks
   (`clean_approve_no_participant_known`). The inbound pipeline always supplies the participant;
   the A3 path relies on the sequence's contact. Pinned as a reminder that the check is only as
   good as the participant the caller passes.
4. **A7 pre-filters are decisive on the fixture and the rules' edges** (new build at
   `year_built = sale year - 1`, 2-year-old home not flagged, entity names exempt from the
   surname rule, a 3000-day hold with a cheap prior sale is not a flip). The model layer only
   adds (`model_adds_flag_prefilter_flags_kept`) and unknown ids / flags are dropped
   (`model_unknown_ids_and_flags_dropped`).
5. **Live evaluation is pending a key** (PRE: `ANTHROPIC_API_KEY`). Classification accuracy,
   extraction quality, A5's judgement of CPY-01 and the model layer of V3 are untested against a
   real model; the harness, the live runner and the skip path are in place.
6. **Mock placeholder caveat (unchanged from BO-09):** an unscripted A4 classify would answer
   `interested` (first enum member) at confidence 0. The harness never relies on the placeholder:
   every case is scripted from its `model` block or synthesised from `expected`.

## Tests

- `tests/evals/test_cases.py` (19): every committed file loads and validates, format detection,
  shims preserve every expected value, coverage minimums (A5 >= 20, A7 >= 12), schema errors.
- `tests/evals/test_scoring.py` (8): normalisation, set-F1, subset, tolerance, text, never-invent.
- `tests/evals/test_runners.py` (26): synthesised outputs are schema-valid for every case,
  representative cases per agent pass end to end, deterministic cases make no model call, a
  wrong expectation fails with a diff, runner exceptions become error results, live mode is
  unavailable without a key, both scripted modes pass every committed case.
- `tests/evals/test_report.py` (8): aggregation, findings, baseline roundtrip, gate semantics.
- `tests/evals/test_cli.py` (11): list, run with gate, report files, `--script-from expected`,
  live skipped (exit 2), `--update-baseline`, regression exit 1 with the new failure printed,
  `--no-gate`, missing baseline and schema errors exit 2.
- `tests/evals/test_regression_gate.py` (3): the CI gate, baseline totals, live run (skipped).
- `tests/reporting/test_learning.py` (14): attribution, template stats, market results and
  thresholds, loader against seeded rows (read-only), activity event + idempotency, workflow and
  schedule registration.

## Decisions

1. One loader, three formats: the older files keep their shapes (their tests depend on them)
   and are migrated in memory. New sets use the unified format.
2. Scripted-from-expected is a mode (`--script-from expected`), not the default: the `model`
   blocks of the older sets deliberately differ from `expected` in places (model flags an
   auto-reply, corrections the comparison cannot see) and those paths deserve coverage too.
3. A case passes only when every check passes; `score` (fraction of checks) is reported for
   triage, not gated. Set checks use F1 at threshold 1.0 (identical sets) unless a case opts
   into `flags_include`.
4. The gate is pass-rate **and** per-id: a stable pass rate with a different failing case is a
   regression. Known failures live in `failing_ids`, so an accepted defect does not block CI.
5. The harness runs on its own in-memory SQLite and never touches the project database; the
   live provider is shared across cases so the breaker and retry state behave as in production.
6. Learning stats are an audit event, not a table: one `learning.stats` row per day is enough
   for the digest and M1 to read, keeps the schema unchanged, and the payload is the dataclass
   document, so a table can be derived later if needed.
7. M1-05 thresholds are not invented: `below_threshold` is `None` until OPN-11 is decided, and
   even then the function only annotates; demotion stays with M1.
