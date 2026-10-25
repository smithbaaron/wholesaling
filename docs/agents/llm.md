# Model layer (`wholesaling.agents.llm`) — build step BO-05

Provider-agnostic model access with per-agent routing and run logging (REQUIREMENTS.md STK-07,
XC-02, XC-06, XC-08, ENG-Q8). v2 names this `agents/llm.py`; it is a package so the import path
`wholesaling.agents.llm` is unchanged and each concern has its own module.

```
wholesaling/agents/llm/
  models.py     ModelRequest / ModelResponse / TaskKind / prompt_hash
  interface.py  ModelProvider protocol + ModelError, RateLimited, ProviderUnavailable, CircuitOpen,
                InvalidRequest, StructuredOutputError
  routing.py    ModelRouter: (agent, task_kind) -> model id
  mock.py       MockModelProvider (deterministic; default mode)
  anthropic.py  AnthropicProvider over the `anthropic` SDK (the only real dependency)
  breaker.py    CircuitBreaker (XC-08)
  pricing.py    PRICES, LONG_CONTEXT_PRICES, estimate_cost
  runner.py     AgentRunner: route -> call -> price -> persist agent_runs
  factory.py    build_provider(settings) / build_runner(settings) keyed on MODEL_PROVIDER
wholesaling/agents/base.py   BaseAgent + PromptTemplate (every agent builds on this)
```

## How a call flows

```
BaseAgent.classify / extract / draft
  -> build_request()            system prompt (PromptTemplate), messages, json_schema, metadata
  -> AgentRunner.run(agent, task_kind, request, session, correlation_id, decision)
       -> ModelRouter.route(agent, task_kind)      model id
       -> prompt_hash(request)                     sha256 over prompt_version + system + messages
                                                   + json_schema + tools (NOT model / max_tokens)
       -> provider.complete(request)               one vendor call (retries + breaker inside)
       -> estimate_cost(model, in, out)            Decimal USD or None for an unknown model
       -> AgentRun row: agent, model, prompt_hash, input_tokens, output_tokens, cost_usd,
          output (parsed object, or {"text", "tool_calls", "stop_reason"}), decision,
          duration_ms (perf_counter), correlation_id, error
```

Every call produces exactly one `agent_runs` row, including failures: on a provider error the row
has `decision="error"`, `error="<ExceptionClass>: <message>"`, zero tokens and zero cost, and is
**committed immediately** (`AgentRunner(commit_on_error=True)`, the default) so it survives the
caller's rollback. Successful rows are only flushed; the caller commits, like every repository in
`wholesaling.db.repositories`.

`agent` and `task_kind` passed to `run()` are authoritative: the request's metadata is rewritten
to match so the routing, the hash and the row can never disagree.

## Routing

Defaults come from settings (`.env.example`):

| Task kind    | Setting             | Default             |
|--------------|---------------------|---------------------|
| `classify`   | `MODEL_CLASSIFY`    | `claude-haiku-5-5`  |
| `extract`    | `MODEL_EXTRACT`     | `claude-haiku-5-5`  |
| `draft`      | `MODEL_DRAFT`       | `claude-sonnet-5-5` |
| `qa`         | `MODEL_QA`          | `claude-sonnet-5-5` |
| `comp_audit` | `MODEL_COMP_AUDIT`  | `claude-sonnet-5-5` |

Routing is by **task kind**, as STK-07 states ("Haiku for classify/extract, Sonnet for drafting,
QA, comp audit"). Section 5's roster lists a single default model per agent (e.g. A4 "Sonnet",
S4 "Sonnet"); where an agent's classify/extract step should nevertheless run on Sonnet, the agent's
build step adds an override (`{"A4": {"classify": "claude-sonnet-5-5"}}`) rather than changing
the task-kind defaults. Decide this per agent in BO-06/BO-07; nothing is overridden today.

Per-agent overrides are a plain dict (a YAML tunable under `config/` later, ENG-Q6), resolved in
this order: `overrides[agent][task_kind]`, then `overrides[agent]["*"]`, then the setting default.

```python
router = ModelRouter.from_settings(
    settings, {"A5": {"qa": "claude-opus-5-5"}, "A2": {"*": "grok-4-mini"}}
)
router.route(AgentId.A5, TaskKind.QA)  # claude-opus-5-5
router.route(AgentId.A2, TaskKind.DRAFT)  # grok-4-mini
```

Unknown agent ids or task kinds in the overrides raise at construction, so a config typo fails at
startup rather than at the first run. `router.table()` returns the routing as data for `/health`.

## Providers

### `MODEL_PROVIDER=mock` (default)

`MockModelProvider` needs no credentials. Resolution order per request:

1. `script_matching(predicate, response)` / `script_containing("needle", response)` — first match.
2. `script(agent, task_kind, text=|parsed=|tool_calls=|raise_error=|responder=)`.
3. Default: when the request has a `json_schema`, a valid object with every `required` key filled
   by a type-appropriate placeholder (`enum[0]`, `const`, `default`, `"mock"`, `0`, `0.0`, `False`,
   `[]`, nested objects recursed, nullable unions take the first non-null type). Without a schema
   it echoes `[mock <task_kind>] <last user message>`.

Tokens are `len(text) // 4` on each side; every request is kept in `provider.requests`. Later
agents' tests therefore run with no scripts at all, and script only the cases they care about.

### `MODEL_PROVIDER=anthropic`

`AnthropicProvider(settings)` requires `ANTHROPIC_API_KEY`. The SDK client is created with
`max_retries=0`; this class owns retries.

- **Structured output**: one tool named `emit_result` whose `input_schema` is the request's
  `json_schema`, forced with `tool_choice={"type": "tool", "name": "emit_result"}`. The tool
  input is `response.parsed`. Works on every model, no beta header. Fallback: the first JSON
  object found in the text (whole string, ```json fence, outermost braces). If neither yields an
  object, `StructuredOutputError` is raised (and the runner logs an error row).
- **User tools** (`ModelRequest.tools`) are passed through with `tool_choice=auto`; calls come
  back as `response.tool_calls`. (Tool execution loops are the agent's business, not the layer's.)
- **Retries**: on 429, 529, any 5xx, 408/409, connection errors and timeouts; exponential backoff
  `0.5s * 2^(attempt-1)` plus up to 50% jitter, capped at 20s, `retry-after` header honoured when
  present; 4 attempts by default. 429/529 exhausted -> `RateLimited(retry_after_s=...)`; 5xx or
  transport exhausted -> `ProviderUnavailable`. Other 4xx -> `InvalidRequest`, no retry, and it
  does **not** count against the breaker (we caused it).
- **Circuit breaker** (`breaker.py`): 5 consecutive failures open it for 30s; while open,
  `complete()` raises `CircuitOpen` without calling the API; after the window one probe is let
  through (half-open); success closes, failure re-opens. Retrying stops as soon as the breaker
  opens. State is per process. `healthcheck()` reports an open breaker as not ok.
- **Logging**: the API key never reaches a log line (`SecretStr` plus the structlog redactor in
  `wholesaling.logging`). Full request bodies and raw responses are logged at DEBUG only; INFO
  carries model, agent, task kind, tokens, latency, stop reason and retry attempts.
- **`temperature` is ignored** on this provider: current models use adaptive thinking and
  `anthropic` SDK 2.x `messages.create` no longer accepts the parameter. The field stays on
  `ModelRequest` for providers that honour it.

### Adding a provider (Grok, OpenAI-compatible, ...)

1. Create `wholesaling/agents/llm/<name>.py` with a class that satisfies `ModelProvider`:
   `name: str`, `async complete(request) -> ModelResponse`, `async healthcheck() -> HealthStatus`.
   Reuse `CircuitBreaker`, the error classes in `interface.py`, and `extract_json_object` from
   `anthropic.py` for the text fallback. For structured output prefer the vendor's native JSON
   schema mode and fall back to a forced tool call as the Anthropic provider does.
2. Add a branch to `factory.build_provider` keyed on the `MODEL_PROVIDER` value (the
   `openai-compatible` value is already reserved in `Settings`; today it raises
   `NotImplementedError` with a pointer here). Import the module lazily inside the branch so the
   default mock path never imports vendor SDKs.
3. Add the provider's model ids and prices to `pricing.PRICES` (and `LONG_CONTEXT_PRICES` when the
   vendor has a threshold tier). An unpriced model does not crash anything: cost is `None`, the
   row stores `0` with `error="unpriced model <id>"`, and one warning is logged per model.
4. Route agents to it with the overrides dict (`{"A2": {"*": "grok-4-mini"}}`) — no code change.
5. Tests: copy `tests/agents/llm/test_anthropic.py`'s fake-client pattern; never hit the network.

## Model ids and prices

Verified **2026-10-25** against Anthropic's models overview
(<https://platform.claude.com/docs/en/about-claude/models/overview>; `docs.claude.com` redirects
there). `.env.example` already had the right ids.

| Model id            | Input $/MTok | Output $/MTok | Notes |
|---------------------|-------------:|--------------:|-------|
| `claude-haiku-5-5`  | 0.10 | 0.50 | Overview says "From $0.10 / From $0.50". Prompts over 100k tokens bill the whole request at 0.50 / 2.50 (read from a secondary write-up of the pricing page, <https://www.eesel.ai/blog/claude-haiku-5-5-pricing>; the pricing page itself, <https://platform.claude.com/docs/en/about-claude/pricing>, was not fetchable from this sandbox — confirm on first live run). |
| `claude-sonnet-5-5` | 2.00 | 10.00 | No long-context premium (<https://apidog.com/blog/claude-sonnet-5-5-pricing/>). |
| `claude-opus-5-5`   | 4.00 | 20.00 | Priced so an override to Opus is costed correctly. |
| `claude-fable-5-1`  | 10.00 | 50.00 | Same reason. |

Dateless ids are pinned snapshots from the 4.6 generation on (overview, "Claude API ID" note), so
there is no `-YYYYMMDD` suffix to track. Retirement: not sooner than Sept/Oct 2027.

`estimate_cost` uses base prices only (no batch discount, no prompt-cache accounting; extend it
when cache token counts are surfaced on `ModelResponse`) and rounds to 6 decimals.

**Known precision gap (for the DB owner):** `agent_runs.cost_usd` is `NUMERIC(12,2)`. A typical
Haiku classification costs ~$0.0003, which persists as `0.00` (verified on SQLite; Postgres rounds
the same way). `ModelResponse.cost_usd` carries the exact value in process, so budget alerts can
sum in memory, but per-run cost in the table is coarse until the column is widened to
`NUMERIC(12,6)` in a later migration (REQUIREMENTS §15 / ENG-Q5 territory, not changed here).

## Writing an agent on `BaseAgent`

```python
from wholesaling.agents.base import BaseAgent, PromptTemplate
from wholesaling.agents.llm import TaskKind
from wholesaling.db.models.enums import AgentId

PROMPT_VERSION = "2026-10-25.1"
SYSTEM_PROMPT = PromptTemplate(
    "You triage replies to investor outreach for {company}. Never promise inventory."
)


class A4ReplyAgent(BaseAgent):
    agent_id = AgentId.A4
    PROMPT_VERSION = PROMPT_VERSION
    SYSTEM_PROMPT = SYSTEM_PROMPT


agent = A4ReplyAgent(
    runner, session, correlation_id=workflow_id, prompt_values={"company": "NorthStack"}
)
result = await agent.classify(reply_text, [c.value for c in MessageClassification])
# result.label, result.confidence, result.rationale, result.run_id
buy_box, run_id = await agent.extract(BUY_BOX_SCHEMA, reply_text)  # Haiku
verdict, run_id = await agent.extract(QA_SCHEMA, draft, task_kind=TaskKind.QA)  # Sonnet
text, run_id = await agent.draft(context, decision="queue_for_human")  # Sonnet
```

- Prompts are module constants. Bump `PROMPT_VERSION` on any wording change: it is hashed into
  `agent_runs.prompt_hash`, so the eval harness (BO-13) can group runs by prompt version.
- `PromptTemplate` (XC-06) refuses placeholder names that look like credentials (`api_key`,
  `secret`, `token`, `password`, `private_key`, `credential`, `bearer`, `client_secret`, and the
  whole words `auth`/`pwd`/`jwt`/`sid`), refuses template text or rendered values that look like
  live secrets (`sk-...`, `Bearer ...`, `AKIA...`, PEM private keys, Slack tokens, JWTs), and
  refuses to render a credential-looking keyword argument. Settings never flow into prompts.
- `classify` validates the label is one of the offered labels and clamps confidence to [0, 1].
- `decision` on `draft`/`run` is the policy engine's verdict for the row (`send`,
  `queue_for_human`, `blocked`); the runner writes `error` itself on failure.

## Tests

`tests/agents/llm/` — 77 tests, all offline: router defaults/overrides, mock schema filling and
scripting, runner persistence (SQLite via the shared `session` fixture; the same suite runs on
Postgres with `TEST_DATABASE_URL`), pricing known/unknown/long-context, breaker state machine,
Anthropic provider with a fake client (structured output via tool and via text fallback, 429
retry-after, 5xx/connection exhaustion, 4xx not retried, breaker opening mid-retry and blocking),
BaseAgent request building, prompt-version hashing, XC-06 template checks.
