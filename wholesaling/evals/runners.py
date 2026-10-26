"""One runner per model-backed agent (BO-13 deliverable 2).

Each runner drives the agent's production code end to end on one golden case: the thread is
built from the case input exactly as the inbound pipeline would hand it over, the agent method
runs (quote stripping, auto-reply detection, A5 pre-checks, unknown tracking, correction
history, A7 pre-filters and all), and the result is scored against ``expected``.

Two provider modes:

- ``mock`` (scripted): a fresh ``MockModelProvider`` per case, scripted with the case's ``model``
  block or, when there is none, with a model output synthesised from ``expected``
  (``model_output_from_expected``). This validates every deterministic step around the model.
- ``anthropic`` (live): the real ``AnthropicProvider``; needs ``ANTHROPIC_API_KEY``. The same
  checks run against what the model actually returns.

Every provider is wrapped in ``RecordingProvider`` so the harness can assert on what was sent
(``model_called``, ``stripped_text_contains`` ...) in both modes.
"""

from __future__ import annotations

import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents import a4 as a4_mod
from wholesaling.agents import s4 as s4_mod
from wholesaling.agents.a4 import A4ReplyAgent, ThreadMessage
from wholesaling.agents.a5 import A5QaAgent, QaDraft, QaInput
from wholesaling.agents.a7 import audit as a7_audit
from wholesaling.agents.a7.audit import A7CompAuditAgent, prefilter_flags
from wholesaling.agents.llm.interface import ModelProvider
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import ModelRequest, ModelResponse, TaskKind
from wholesaling.agents.llm.routing import ModelRouter, Overrides
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.s4 import S4IntakeAgent
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria
from wholesaling.db.repositories.sellers import SellerRepository, missing_required, unasked_optional
from wholesaling.evals.cases import GoldenCase
from wholesaling.evals.scoring import (
    Check,
    contains,
    exact,
    excludes,
    never_invented,
    number_within,
    score_of,
    set_f1,
    subset,
)
from wholesaling.integrations.base import HealthStatus
from wholesaling.integrations.data.property import SoldComp

ProviderMode = Literal["mock", "anthropic"]
ScriptSource = Literal["case", "expected"]
"""Where the scripted model output comes from: the case's ``model`` block when it has one
(``case``), or always synthesised from ``expected`` (``expected``)."""

NOW: Final = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
OUR_ADDRESS: Final = "deals@example.test"
INVESTOR_ADDRESS: Final = "jane@acme.com"
PROMPT_COMPANY: Final = "NorthStack"

EVAL_OVERRIDES: Final[Overrides] = {
    **{k: dict(v) for k, v in a4_mod.MODEL_OVERRIDES.items()},
    **{k: dict(v) for k, v in a7_audit.MODEL_OVERRIDES.items()},
}
"""The per-agent routing the production contexts apply (A4 classify and A7 comp audit on Sonnet)."""

# S4 fields the test suite treats as "never invented" when the seller did not state them.
S4_NULLABLE_FIELDS: Final[tuple[str, ...]] = (
    "motivation",
    "condition_class",
    "timeline",
    "asking_price",
    "co_owners",
    "occupancy",
    "liens",
    "hoa",
)


class EvalUnavailable(RuntimeError):
    """The requested provider cannot run here (no API key)."""


# --------------------------------------------------------------------------------------------
# Provider plumbing
# --------------------------------------------------------------------------------------------


class RecordingProvider:
    """Delegates to a provider and keeps every request, whatever the provider is."""

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner
        self.name: str = inner.name
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await self._inner.complete(request)

    async def healthcheck(self) -> HealthStatus:
        return await self._inner.healthcheck()

    def requests_for(self, task_kind: TaskKind) -> list[ModelRequest]:
        return [r for r in self.requests if r.metadata.task_kind is task_kind]


def live_available(settings: Settings) -> bool:
    return settings.anthropic_api_key is not None and bool(
        settings.anthropic_api_key.get_secret_value()
    )


def build_live_provider(settings: Settings) -> ModelProvider:
    if not live_available(settings):
        msg = "ANTHROPIC_API_KEY is not set; live evals cannot run (use --provider mock)"
        raise EvalUnavailable(msg)
    from wholesaling.agents.llm.anthropic import AnthropicProvider

    return AnthropicProvider(settings)


@dataclass(slots=True)
class RunnerContext:
    session: AsyncSession
    settings: Settings
    mode: ProviderMode = "mock"
    live_provider: ModelProvider | None = None
    script_source: ScriptSource = "case"

    def provider_for(self, case: GoldenCase) -> RecordingProvider:
        if self.mode == "mock":
            mock = MockModelProvider()
            script_case(mock, case, source=self.script_source)
            return RecordingProvider(mock)
        if self.live_provider is None:
            self.live_provider = build_live_provider(self.settings)
        return RecordingProvider(self.live_provider)

    def runner(self, provider: ModelProvider) -> AgentRunner:
        return AgentRunner(provider, ModelRouter.from_settings(self.settings, EVAL_OVERRIDES))


# --------------------------------------------------------------------------------------------
# Scripting: the case's model block, else synthesised from expected
# --------------------------------------------------------------------------------------------


def _geography_item(box: Mapping[str, Any]) -> dict[str, Any] | None:
    geo: dict[str, Any] = {"markets": None, "cities": None, "zips": None, "neighborhoods": None}
    nested = box.get("geography")
    if isinstance(nested, Mapping):
        for key in geo:
            if nested.get(key):
                geo[key] = list(nested[key])
    for key in geo:
        if box.get(key):
            geo[key] = list(box[key])
    return geo if any(v for v in geo.values()) else None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(Decimal(str(value)))


def _a4_extract_item(box: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "buy_box_key": str(box.get("buy_box_key") or "default"),
        "geography": _geography_item(box),
        "asset_types": list(box["asset_types"]) if box.get("asset_types") else None,
        "price_min": _number(box.get("price_min")),
        "price_max": _number(box.get("price_max")),
        "condition": box.get("condition"),
        "strategy": box.get("strategy"),
        "financing": box.get("financing"),
        "closing_timeline": box.get("closing_timeline"),
        "other_criteria": [
            {"name": str(k), "value": str(v)} for k, v in (box.get("other_criteria") or {}).items()
        ],
        "corrections": [],
        "raw_text": box.get("raw_text"),
    }


def model_output_from_expected(case: GoldenCase) -> dict[str, dict[str, Any]]:
    """Synthesise the structured model output(s) a perfect model would return, keyed by task
    kind value. Used when a case carries no ``model`` block (scripted-from-expected mode)."""
    exp = case.expected
    if case.agent == "A4" and case.task == "classify":
        return {
            TaskKind.CLASSIFY.value: {
                "classification": exp.get("classification", "ambiguous"),
                "confidence": float(exp.get("confidence", 0.9)),
                "rationale": "synthesised from expected",
                "is_auto_reply": bool(exp.get("is_auto_reply", False)),
                "reconnect_date": exp.get("reconnect_date"),
            }
        }
    if case.agent == "A4" and case.task == "extract":
        return {
            TaskKind.EXTRACT.value: {
                "buy_boxes": [_a4_extract_item(b) for b in exp.get("buy_boxes") or []],
                "rationale": "synthesised from expected",
            }
        }
    if case.agent == "A4" and case.task == "draft":
        return {
            TaskKind.DRAFT.value: {
                "subject": str(exp.get("subject") or ""),
                "body": str(exp.get("body") or ""),
                "asks_for": list(exp.get("asks_for") or []),
                "contains_sensitive": bool(exp.get("model_contains_sensitive", False)),
            }
        }
    if case.agent == "A5":
        return {
            TaskKind.QA.value: {
                "verdict": exp.get("verdict", "approve"),
                "confidence": float(exp.get("confidence", 0.9)),
                "flags": list(exp.get("model_flags") or []),
                "rewrite_body": exp.get("rewrite_body"),
                "rationale": "synthesised from expected",
            }
        }
    if case.agent == "S4":
        fields = dict(exp.get("fields") or {})
        extract: dict[str, Any] = {
            key: None
            for key in s4_mod.EXTRACT_SCHEMA["properties"]
            if key not in ("disclosed_issues", "seller_unsure", "photos_mentioned", "rationale")
        }
        extract.update({k: v for k, v in fields.items() if k in extract})
        if "asking_price" in fields:
            extract["asking_price"] = _number(fields["asking_price"])
        extract["disclosed_issues"] = list(fields.get("disclosed_issues") or [])
        extract["seller_unsure"] = list(exp.get("seller_unsure") or [])
        extract["photos_mentioned"] = bool(fields.get("photos_mentioned", False))
        extract["rationale"] = "synthesised from expected"
        return {
            TaskKind.CLASSIFY.value: {
                "classification": exp.get("classification", "reply"),
                "confidence": float(exp.get("confidence", 0.9)),
                "rationale": "synthesised from expected",
                "is_auto_reply": bool(exp.get("is_auto_reply", False)),
                "asks_offer": bool(exp.get("asks_offer", False)),
            },
            TaskKind.EXTRACT.value: extract,
        }
    if case.agent == "A7":
        model_flags: Mapping[str, Any] = exp.get("model_flags") or {}
        return {
            TaskKind.COMP_AUDIT.value: {
                "verdicts": [
                    {
                        "comp_id": cid,
                        "flags": list(flags),
                        "reasons": [f"synthesised: {f}" for f in flags],
                    }
                    for cid, flags in model_flags.items()
                ],
                "rationale": "synthesised from expected",
            }
        }
    msg = f"no synthesiser for {case.agent}/{case.task}"
    raise ValueError(msg)


def scripted_outputs(
    case: GoldenCase, *, source: ScriptSource = "case"
) -> dict[str, dict[str, Any]]:
    """What the scripted provider answers per task kind for this case."""
    if case.model is None or source == "expected":
        return model_output_from_expected(case)
    if case.agent == "S4":
        out: dict[str, dict[str, Any]] = {}
        if isinstance(case.model.get("classify"), Mapping):
            out[TaskKind.CLASSIFY.value] = dict(case.model["classify"])
        if isinstance(case.model.get("extract"), Mapping):
            out[TaskKind.EXTRACT.value] = dict(case.model["extract"])
        synthesised = model_output_from_expected(case)
        for key, value in synthesised.items():
            out.setdefault(key, value)
        return out
    task_kind = {
        ("A4", "classify"): TaskKind.CLASSIFY,
        ("A4", "extract"): TaskKind.EXTRACT,
        ("A4", "draft"): TaskKind.DRAFT,
        ("A5", "qa"): TaskKind.QA,
        ("A7", "comp_audit"): TaskKind.COMP_AUDIT,
    }[(case.agent, case.task)]
    return {task_kind.value: dict(case.model)}


def script_case(
    provider: MockModelProvider, case: GoldenCase, *, source: ScriptSource = "case"
) -> None:
    agent = AgentId(case.agent)
    for task_kind, parsed in scripted_outputs(case, source=source).items():
        provider.script(agent, TaskKind(task_kind), parsed=parsed)


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CaseResult:
    case_id: str
    agent: str
    task: str
    mode: ProviderMode
    checks: list[Check] = field(default_factory=list)
    model_calls: int = 0
    error: str | None = None
    tags: tuple[str, ...] = ()
    notes: str = ""
    source: str = ""

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    @property
    def score(self) -> float:
        return 0.0 if self.error else score_of(self.checks)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "agent": self.agent,
            "task": self.task,
            "mode": self.mode,
            "passed": self.passed,
            "score": round(self.score, 4),
            "model_calls": self.model_calls,
            "error": self.error,
            "tags": list(self.tags),
            "notes": self.notes,
            "source": self.source,
            "checks": [c.to_json() for c in self.checks],
        }

    def diff(self) -> str:
        lines = [f"{'PASS' if self.passed else 'FAIL'} {self.agent}/{self.task} {self.case_id}"]
        if self.error:
            lines.append(f"  error: {self.error}")
        lines.extend(f"  {c.diff()}" for c in self.checks if not c.passed)
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Thread builders (the projection the pipeline hands the agents)
# --------------------------------------------------------------------------------------------


def build_thread(
    inp: Mapping[str, Any], *, counterpart: str, subject_on_history: bool = True
) -> list[ThreadMessage]:
    """``history`` (oldest first) + the latest inbound message, as the pipeline renders them."""
    out: list[ThreadMessage] = []
    for i, h in enumerate(inp.get("history") or []):
        direction = str(h["direction"])
        out.append(
            ThreadMessage(
                direction=direction,
                body=str(h["body"]),
                from_address=str(
                    h.get("from") or (OUR_ADDRESS if direction == "out" else counterpart)
                ),
                subject=str(inp.get("subject") or "") if subject_on_history else h.get("subject"),
                at=NOW - timedelta(days=5) + timedelta(days=i),
            )
        )
    out.append(
        ThreadMessage(
            direction="in",
            body=str(inp["body"]),
            from_address=counterpart,
            subject=inp.get("subject"),
            at=NOW,
            headers=dict(inp.get("headers") or {}),
            is_latest_inbound=True,
        )
    )
    return out


def criteria_from_golden(doc: Mapping[str, Any]) -> BuyBoxCriteria:
    return BuyBoxCriteria(
        geography=doc.get("geography"),
        asset_types=doc.get("asset_types"),
        price_min=Decimal(str(doc["price_min"])) if doc.get("price_min") else None,
        price_max=Decimal(str(doc["price_max"])) if doc.get("price_max") else None,
        condition=doc.get("condition"),
        strategy=doc.get("strategy"),
        financing=doc.get("financing"),
        closing_preferences=doc.get("closing_preferences"),
        other_criteria=doc.get("other_criteria"),
    )


def _latest_prompt_part(request: ModelRequest) -> str:
    sent = request.messages[-1].content
    return sent.split("LATEST INBOUND MESSAGE", 1)[-1]


def _prompt_checks(expected: Mapping[str, Any], request: ModelRequest | None) -> list[Check]:
    checks: list[Check] = []
    if request is None:
        return checks
    sent = request.messages[-1].content
    if expected.get("stripped_text_contains"):
        checks.append(
            contains("stripped_text_contains", str(expected["stripped_text_contains"]), sent)
        )
    if expected.get("stripped_text_excludes"):
        checks.append(
            excludes(
                "stripped_text_excludes",
                str(expected["stripped_text_excludes"]),
                _latest_prompt_part(request),
            )
        )
    return checks


# --------------------------------------------------------------------------------------------
# A4
# --------------------------------------------------------------------------------------------


async def run_a4_classify(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    result = _new_result(case, ctx)
    provider = ctx.provider_for(case)
    agent = A4ReplyAgent(
        ctx.runner(provider),
        ctx.session,
        correlation_id=f"eval:{case.id}",
        prompt_values={"company": PROMPT_COMPANY},
    )
    thread = build_thread(case.input, counterpart=INVESTOR_ADDRESS)
    got = await agent.classify(thread)
    exp = case.expected
    checks = result.checks
    checks.append(exact("classification", exp.get("classification"), got.classification.value))
    checks.append(exact("is_auto_reply", bool(exp.get("is_auto_reply", False)), got.is_auto_reply))
    if "quoted_text_stripped" in exp:
        checks.append(
            exact("quoted_text_stripped", exp["quoted_text_stripped"], got.quoted_text_stripped)
        )
    if "reconnect_date" in exp:
        want = exp["reconnect_date"]
        checks.append(
            exact(
                "reconnect_date",
                date.fromisoformat(want) if want else None,
                got.reconnect_date,
            )
        )
    calls = provider.requests_for(TaskKind.CLASSIFY)
    result.model_calls = len(provider.requests)
    if "model_called" in exp:
        checks.append(exact("model_called", bool(exp["model_called"]), bool(calls)))
    if not calls:
        checks.append(exact("deterministic_confidence", 1.0, got.confidence))
        checks.append(exact("no_run_id_without_model", None, got.run_id))
    else:
        checks.append(exact("run_logged", True, got.run_id is not None))
    checks.extend(_prompt_checks(exp, calls[0] if calls else None))
    return result


def _tracked_values(criteria: BuyBoxCriteria) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in a4_mod.TRACKED_BUY_BOX_FIELDS:
        if name == "closing_timeline":
            out[name] = (criteria.closing_preferences or {}).get("timeline")
        else:
            out[name] = getattr(criteria, name)
    return out


async def run_a4_extract(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    result = _new_result(case, ctx)
    provider = ctx.provider_for(case)
    agent = A4ReplyAgent(
        ctx.runner(provider),
        ctx.session,
        correlation_id=f"eval:{case.id}",
        prompt_values={"company": PROMPT_COMPANY},
    )
    thread = build_thread(case.input, counterpart=INVESTOR_ADDRESS)
    current = {
        str(k): criteria_from_golden(v) for k, v in (case.input.get("current") or {}).items()
    }
    got = await agent.extract_buy_box(thread, current)
    exp_boxes: Sequence[Mapping[str, Any]] = case.expected.get("buy_boxes") or []
    checks = result.checks
    checks.append(
        exact(
            "buy_box_keys",
            [b.get("buy_box_key", "default") for b in exp_boxes],
            [b.buy_box_key for b in got.buy_boxes],
        )
    )
    for want, box in zip(exp_boxes, got.buy_boxes, strict=False):
        key = str(want.get("buy_box_key", "default"))
        c = box.criteria
        if "price_max" in want:
            checks.append(number_within(f"{key}.price_max", want["price_max"], c.price_max))
        if "price_min" in want:
            checks.append(number_within(f"{key}.price_min", want["price_min"], c.price_min))
        if "asset_types" in want:
            checks.append(set_f1(f"{key}.asset_types", want["asset_types"], c.asset_types or []))
        for geo_key in ("markets", "cities", "zips", "neighborhoods"):
            if geo_key in want:
                checks.append(
                    set_f1(f"{key}.{geo_key}", want[geo_key], (c.geography or {}).get(geo_key, []))
                )
        for scalar in ("financing", "condition", "strategy"):
            if scalar in want:
                checks.append(exact(f"{key}.{scalar}", want[scalar], getattr(c, scalar)))
        if "closing_timeline" in want:
            checks.append(
                exact(
                    f"{key}.closing_timeline",
                    want["closing_timeline"],
                    (c.closing_preferences or {}).get("timeline"),
                )
            )
        if "unknowns" in want:
            checks.append(exact(f"{key}.unknowns", want["unknowns"], box.unknowns))
            checks.append(exact(f"{key}.unknown_fields", want["unknowns"], c.unknown_fields))
            checks.append(
                never_invented(f"{key}.never_invent", want["unknowns"], _tracked_values(c))
            )
        if "corrections" in want:
            checks.append(
                exact(
                    f"{key}.corrections",
                    want["corrections"],
                    [x.to_json() for x in box.corrections],
                )
            )
    result.model_calls = len(provider.requests)
    checks.append(exact("model_called", True, bool(provider.requests_for(TaskKind.EXTRACT))))
    return result


# --------------------------------------------------------------------------------------------
# A5
# --------------------------------------------------------------------------------------------


def _qa_thread(inp: Mapping[str, Any], participant: str | None) -> list[ThreadMessage]:
    out: list[ThreadMessage] = []
    raw: Sequence[Mapping[str, Any]] = inp.get("thread") or []
    inbound = [m for m in raw if m.get("direction") == "in"]
    for i, m in enumerate(raw):
        direction = str(m["direction"])
        hours_ago = m.get("hours_ago")
        at = (
            NOW - timedelta(hours=float(hours_ago))
            if hours_ago is not None
            else NOW - timedelta(days=len(raw) - i)
        )
        out.append(
            ThreadMessage(
                direction=direction,
                body=str(m["body"]),
                from_address=str(
                    m.get("from") or (OUR_ADDRESS if direction == "out" else participant or "")
                )
                or None,
                subject=m.get("subject"),
                at=at,
                is_latest_inbound=bool(direction == "in" and inbound and m is inbound[-1]),
            )
        )
    return out


def qa_input_from_case(case: GoldenCase) -> QaInput:
    inp = case.input
    draft_doc: Mapping[str, Any] = inp["draft"]
    participant = inp.get("thread_participant")
    last_hours = inp.get("last_outbound_hours_ago")
    return QaInput(
        draft=QaDraft(
            to=list(draft_doc.get("to") or []),
            subject=str(draft_doc.get("subject") or ""),
            body=str(draft_doc.get("body") or ""),
            asks_for=list(draft_doc.get("asks_for") or []),
            contains_sensitive=bool(draft_doc.get("contains_sensitive", False)),
        ),
        thread=_qa_thread(inp, participant),
        facts=dict(inp.get("facts") or {}),
        current_buy_box=dict(inp["current_buy_box"]) if inp.get("current_buy_box") else None,
        recipient_suppressed=bool(inp.get("recipient_suppressed", False)),
        thread_participant=participant,
        last_outbound_asks_for=list(inp.get("last_outbound_asks_for") or []),
        last_outbound_at=(
            NOW - timedelta(hours=float(last_hours)) if last_hours is not None else None
        ),
        now=NOW,
        scheduled_sequence=bool(inp.get("scheduled_sequence", False)),
    )


async def run_a5_qa(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    result = _new_result(case, ctx)
    provider = ctx.provider_for(case)
    agent = A5QaAgent(
        ctx.runner(provider),
        ctx.session,
        correlation_id=f"eval:{case.id}",
        prompt_values={"company": PROMPT_COMPANY},
    )
    qa = qa_input_from_case(case)
    got = await agent.review(qa)
    exp = case.expected
    checks = result.checks
    checks.append(exact("verdict", exp.get("verdict"), got.verdict.value))
    if "flags" in exp:
        checks.append(set_f1("flags", exp["flags"], got.flags))
    if "flags_include" in exp:
        checks.append(subset("flags_include", exp["flags_include"], got.flags))
    calls = provider.requests_for(TaskKind.QA)
    result.model_calls = len(provider.requests)
    if "model_called" in exp:
        checks.append(exact("model_called", bool(exp["model_called"]), bool(calls)))
        checks.append(exact("precheck_only", not bool(exp["model_called"]), got.precheck_only))
    if not calls:
        checks.append(exact("precheck_confidence", 1.0, got.confidence))
    if "min_confidence" in exp:
        checks.append(
            Check(
                "min_confidence",
                got.confidence >= float(exp["min_confidence"]),
                f">= {exp['min_confidence']}",
                got.confidence,
            )
        )
    if "has_rewrite_body" in exp:
        checks.append(
            exact("has_rewrite_body", bool(exp["has_rewrite_body"]), got.rewrite_body is not None)
        )
    if exp.get("rewrite_body_excludes"):
        checks.append(
            excludes(
                "rewrite_body_excludes",
                str(exp["rewrite_body_excludes"]),
                got.rewrite_body or "",
            )
        )
    return result


# --------------------------------------------------------------------------------------------
# S4
# --------------------------------------------------------------------------------------------


async def _seller_lead_and_intake(
    session: AsyncSession, verified: bool
) -> tuple[SellerLead, Intake]:
    lead = SellerLead(
        owner_name="Dolores Ramirez",
        property_address_line1="1420 Pecan Grove Dr",
        property_city="Houston",
        property_state="TX",
        property_postal_code="77009",
        parcel_id="HCAD-D-0000001" if verified else None,
        parcel_verified=verified,
    )
    session.add(lead)
    await session.flush()
    intake = await SellerRepository(session).get_or_create_intake(lead)
    return lead, intake


async def run_s4_intake(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    result = _new_result(case, ctx)
    provider = ctx.provider_for(case)
    agent = S4IntakeAgent(
        ctx.runner(provider),
        ctx.session,
        correlation_id=f"eval:{case.id}",
        prompt_values={"company": PROMPT_COMPANY},
    )
    inp = case.input
    seller = str(inp.get("from") or "seller@example.test")
    thread = build_thread(inp, counterpart=seller, subject_on_history=False)
    exp = case.expected
    checks = result.checks

    got = await agent.classify(thread)
    checks.append(exact("classification", exp.get("classification"), got.classification.value))
    checks.append(exact("is_auto_reply", bool(exp.get("is_auto_reply", False)), got.is_auto_reply))
    if "asks_offer" in exp:
        checks.append(exact("asks_offer", bool(exp["asks_offer"]), got.asks_offer))
    if "quoted_text_stripped" in exp:
        checks.append(
            exact("quoted_text_stripped", exp["quoted_text_stripped"], got.quoted_text_stripped)
        )
    classify_calls = provider.requests_for(TaskKind.CLASSIFY)
    if "model_called" in exp:
        checks.append(exact("model_called", bool(exp["model_called"]), bool(classify_calls)))
    if not classify_calls:
        checks.append(exact("deterministic_confidence", 1.0, got.confidence))
    checks.extend(_prompt_checks(exp, classify_calls[0] if classify_calls else None))

    if exp.get("extract_called"):
        checks.append(exact("warrants_intake", True, got.warrants_intake))
        known: Mapping[str, Any] = inp.get("known") or {}
        verified = bool(known.get("parcel_verified", True))
        lead, intake = await _seller_lead_and_intake(ctx.session, verified)
        extraction = await agent.extract(thread, {"property_address": lead.property_address_line1})
        fields = extraction.fields.to_json()
        want_fields: Mapping[str, Any] = exp.get("fields") or {}
        for name, value in want_fields.items():
            if name == "asking_price":
                checks.append(number_within("fields.asking_price", value, fields.get(name)))
            elif isinstance(value, list):
                checks.append(set_f1(f"fields.{name}", value, fields.get(name) or []))
            else:
                checks.append(exact(f"fields.{name}", value, fields.get(name)))
        checks.append(
            never_invented(
                "never_invent",
                [f for f in S4_NULLABLE_FIELDS if f not in want_fields],
                fields,
            )
        )
        repo = SellerRepository(ctx.session)
        await repo.merge_intake(intake, extraction.fields)
        if "missing_required" in exp:
            checks.append(
                exact("missing_required", exp["missing_required"], missing_required(lead, intake))
            )
        if "seller_unsure" in exp:
            checks.append(
                exact(
                    "seller_unsure",
                    exp["seller_unsure"],
                    (intake.property_facts or {}).get("seller_unsure", []),
                )
            )
            unasked = unasked_optional(intake)
            checks.append(
                Check(
                    "unsure_not_reasked",
                    not any(name in unasked for name in exp["seller_unsure"]),
                    [],
                    [n for n in exp["seller_unsure"] if n in unasked],
                )
            )
        for name in ("co_owners", "occupancy", "liens", "hoa"):
            stated = name in want_fields
            checks.append(exact(f"{name}_unknown", not stated, getattr(intake, f"{name}_unknown")))
        checks.append(exact("extract_called", True, bool(provider.requests_for(TaskKind.EXTRACT))))
    result.model_calls = len(provider.requests)
    return result


# --------------------------------------------------------------------------------------------
# A7 V3 comp audit
# --------------------------------------------------------------------------------------------


def comps_from_case(case: GoldenCase) -> list[SoldComp]:
    return [SoldComp.from_json(dict(c)) for c in case.input.get("comps") or []]


async def run_a7_comp_audit(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    result = _new_result(case, ctx)
    provider = ctx.provider_for(case)
    agent = A7CompAuditAgent(
        ctx.runner(provider),
        ctx.session,
        correlation_id=f"eval:{case.id}",
        prompt_values={"company": PROMPT_COMPANY},
    )
    comps = comps_from_case(case)
    as_of_raw = case.input.get("as_of")
    as_of = date.fromisoformat(str(as_of_raw)) if as_of_raw else None
    subject: Mapping[str, Any] = case.input.get("subject") or {}
    exp = case.expected
    checks = result.checks

    pre = prefilter_flags(comps)
    want_pre: Mapping[str, Any] = exp.get("prefilter") or {}
    for c in comps:
        checks.append(
            set_f1(f"prefilter.{c.comp_id}", want_pre.get(c.comp_id, []), pre[c.comp_id].flags)
        )

    got = await agent.audit(subject, comps, as_of=as_of)
    want_flagged: Mapping[str, Any] = exp.get("flagged") or {}
    checks.append(set_f1("flagged_ids", list(want_flagged), got.flagged_ids))
    for c in comps:
        verdict = got.verdicts[c.comp_id]
        checks.append(set_f1(f"flags.{c.comp_id}", want_flagged.get(c.comp_id, []), verdict.flags))
        checks.append(subset(f"keeps_prefilter.{c.comp_id}", pre[c.comp_id].flags, verdict.flags))
    for cid, flags in want_flagged.items():
        if cid not in got.verdicts:
            checks.append(Check(f"flags.{cid}", False, flags, None, "comp id not in the set"))
    result.model_calls = len(provider.requests)
    audited = bool(provider.requests_for(TaskKind.COMP_AUDIT))
    checks.append(exact("model_called", bool(comps), audited))
    return result


# --------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------


Runner = Any  # Callable[[GoldenCase, RunnerContext], Awaitable[CaseResult]]

RUNNERS: Final[dict[tuple[str, str], Runner]] = {
    ("A4", "classify"): run_a4_classify,
    ("A4", "extract"): run_a4_extract,
    ("A5", "qa"): run_a5_qa,
    ("S4", "intake"): run_s4_intake,
    ("A7", "comp_audit"): run_a7_comp_audit,
}


def _new_result(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        agent=case.agent,
        task=case.task,
        mode=ctx.mode,
        tags=case.tags,
        notes=case.notes,
        source=case.source,
    )


async def run_case(case: GoldenCase, ctx: RunnerContext) -> CaseResult:
    """Run one case; an exception becomes ``error`` on the result instead of propagating."""
    runner = RUNNERS.get((case.agent, case.task))
    if runner is None:
        result = _new_result(case, ctx)
        result.error = f"no runner for {case.agent}/{case.task}"
        return result
    try:
        out: CaseResult = await runner(case, ctx)
    except EvalUnavailable:
        raise
    except Exception as exc:
        result = _new_result(case, ctx)
        result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
        return result
    return out


__all__ = [
    "EVAL_OVERRIDES",
    "NOW",
    "RUNNERS",
    "CaseResult",
    "EvalUnavailable",
    "ProviderMode",
    "RecordingProvider",
    "RunnerContext",
    "ScriptSource",
    "build_live_provider",
    "build_thread",
    "comps_from_case",
    "criteria_from_golden",
    "live_available",
    "model_output_from_expected",
    "qa_input_from_case",
    "run_a4_classify",
    "run_a4_extract",
    "run_a5_qa",
    "run_a7_comp_audit",
    "run_case",
    "run_s4_intake",
    "script_case",
    "scripted_outputs",
]


# --------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------


async def run_all(
    cases: Sequence[GoldenCase],
    *,
    settings: Settings,
    mode: ProviderMode = "mock",
    script_source: ScriptSource = "case",
    database_url: str = "sqlite+aiosqlite:///:memory:",
) -> list[CaseResult]:
    """Run every case on a fresh database (each case in its own rolled-back session).

    ``agent_runs`` rows are written by the runner like in production and discarded with the
    rollback; the harness never needs the project database.
    """
    import wholesaling.db.models  # noqa: F401 - populate Base.metadata
    from wholesaling.db import Base, make_engine, make_session_factory

    engine = make_engine(database_url)
    results: list[CaseResult] = []
    live: ModelProvider | None = build_live_provider(settings) if mode == "anthropic" else None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = make_session_factory(engine)
        for case in cases:
            async with factory() as session:
                ctx = RunnerContext(
                    session=session,
                    settings=settings,
                    mode=mode,
                    live_provider=live,
                    script_source=script_source,
                )
                results.append(await run_case(case, ctx))
                await session.rollback()
    finally:
        await engine.dispose()
    return results


__all__ += ["run_all"]
