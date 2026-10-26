"""Runners: scripted model outputs are schema-valid, every agent's deterministic path runs end
to end on its golden cases, a wrong expectation fails with a diff, errors become findings, and
live mode is skipped cleanly without a key (BO-13 #2)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.evals.conftest import case_by_id
from wholesaling.agents import a4, a5, s4
from wholesaling.agents.a7 import audit
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId
from wholesaling.evals.cases import GoldenCase
from wholesaling.evals.runners import (
    EVAL_OVERRIDES,
    RUNNERS,
    EvalUnavailable,
    RecordingProvider,
    RunnerContext,
    live_available,
    model_output_from_expected,
    qa_input_from_case,
    run_all,
    run_case,
    script_case,
    scripted_outputs,
)

SCHEMAS = {
    ("A4", TaskKind.CLASSIFY.value): a4.CLASSIFY_SCHEMA,
    ("A4", TaskKind.EXTRACT.value): a4.EXTRACT_SCHEMA,
    ("A5", TaskKind.QA.value): a5.QA_SCHEMA,
    ("S4", TaskKind.CLASSIFY.value): s4.CLASSIFY_SCHEMA,
    ("S4", TaskKind.EXTRACT.value): s4.EXTRACT_SCHEMA,
    ("A7", TaskKind.COMP_AUDIT.value): audit.AUDIT_SCHEMA,
}


def _check_against_schema(value: Any, schema: dict[str, Any], path: str) -> None:
    """A small structural validator: required keys present, no extra keys, enums honoured."""
    if "anyOf" in schema:
        if value is None and any(o.get("type") == "null" for o in schema["anyOf"]):
            return
        _check_against_schema(
            value, next(o for o in schema["anyOf"] if o.get("type") != "null"), path
        )
        return
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict), path
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        assert not missing, f"{path}: missing {missing}"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{path}: extra {extra}"
        for k, v in value.items():
            if k in props:
                _check_against_schema(v, props[k], f"{path}.{k}")
    elif kind == "array":
        assert isinstance(value, list), path
        for i, item in enumerate(value):
            _check_against_schema(item, schema.get("items", {}), f"{path}[{i}]")
    elif kind == "string":
        assert isinstance(value, str), f"{path}: {value!r} is not a string"
        if "enum" in schema:
            assert value in schema["enum"], f"{path}: {value!r} not in enum"
    elif kind == "number":
        assert isinstance(value, int | float), f"{path}: {value!r} is not a number"
    elif kind == "boolean":
        assert isinstance(value, bool), f"{path}: {value!r} is not a bool"


def test_synthesised_model_output_is_schema_valid_for_every_case(
    all_cases: list[GoldenCase],
) -> None:
    for case in all_cases:
        for task_kind, parsed in model_output_from_expected(case).items():
            _check_against_schema(
                parsed, SCHEMAS[(case.agent, task_kind)], f"{case.id}:{task_kind}"
            )


def test_scripted_outputs_prefer_the_case_model_block(all_cases: list[GoldenCase]) -> None:
    case = case_by_id(all_cases, "interested_plain", "A4")
    assert case.model is not None
    assert scripted_outputs(case)[TaskKind.CLASSIFY.value] == dict(case.model)
    synthesised = scripted_outputs(case, source="expected")[TaskKind.CLASSIFY.value]
    assert synthesised["classification"] == "interested" and synthesised != dict(case.model)
    s4_case = case_by_id(all_cases, "full_info", "S4")
    outputs = scripted_outputs(s4_case)
    assert set(outputs) == {TaskKind.CLASSIFY.value, TaskKind.EXTRACT.value}
    assert outputs[TaskKind.EXTRACT.value]["asking_price"] == 185000


def test_script_case_registers_each_task_kind(all_cases: list[GoldenCase]) -> None:
    provider = MockModelProvider()
    script_case(provider, case_by_id(all_cases, "full_info", "S4"))
    assert set(provider._scripts) == {("S4", "classify"), ("S4", "extract")}


def test_eval_overrides_route_like_production() -> None:
    assert EVAL_OVERRIDES[AgentId.A4.value][TaskKind.CLASSIFY.value] == "claude-sonnet-5-5"
    assert EVAL_OVERRIDES["A7"]["comp_audit"] == "claude-sonnet-5-5"


def test_every_agent_task_has_a_runner() -> None:
    from wholesaling.evals.cases import AGENT_TASKS

    for agent, tasks in AGENT_TASKS.items():
        for task in tasks:
            if (agent, task) == ("A4", "draft"):
                continue  # schema-only today: no golden draft cases are committed
            assert (agent, task) in RUNNERS


@pytest.mark.parametrize(
    ("case_id", "agent"),
    [
        ("quoted_outlook_original_message", "A4"),
        ("out_of_office_subject", "A4"),
        ("correction_price_max:extract", "A4"),
        ("buy_box_two_in_one_email:extract", "A4"),
        ("prohibited_inventory", "A5"),
        ("sensitive_contract_question_rewrite", "A5"),
        ("clean_approve_asks_missing_field", "A5"),
        ("full_info", "S4"),
        ("unknown_liens", "S4"),
        ("auto_reply", "S4"),
        ("fixture_dirty_four_flags", "A7"),
        ("model_adds_flag_prefilter_flags_kept", "A7"),
        ("empty_set_no_model_call", "A7"),
    ],
)
async def test_representative_cases_pass_in_scripted_mode(
    case_id: str, agent: str, all_cases: list[GoldenCase], session: AsyncSession, settings: Settings
) -> None:
    case = case_by_id(all_cases, case_id, agent)
    result = await run_case(case, RunnerContext(session=session, settings=settings))
    assert result.passed, result.diff()
    assert result.score == 1.0 and result.error is None
    assert result.checks, "every runner produces checks"
    expects_model = case.expected.get("model_called", True)
    if agent == "A7":
        expects_model = bool(case.input["comps"])
    assert (result.model_calls > 0) is bool(expects_model)


async def test_deterministic_cases_never_reach_the_model(
    all_cases: list[GoldenCase], session: AsyncSession, settings: Settings
) -> None:
    for case_id, agent in [
        ("out_of_office_subject", "A4"),
        ("cadence_within_24h", "A5"),
        ("auto_reply", "S4"),
    ]:
        case = case_by_id(all_cases, case_id, agent)
        result = await run_case(case, RunnerContext(session=session, settings=settings))
        assert result.passed and result.model_calls == 0, result.diff()


async def test_wrong_expectation_fails_with_a_diff(
    all_cases: list[GoldenCase], session: AsyncSession, settings: Settings
) -> None:
    base = case_by_id(all_cases, "prohibited_inventory", "A5")
    wrong = dataclasses.replace(base, expected={**base.expected, "verdict": "approve"})
    result = await run_case(wrong, RunnerContext(session=session, settings=settings))
    assert not result.passed and 0 < result.score < 1
    names = {c.name for c in result.failed_checks}
    assert names == {"verdict"}
    assert "FAIL verdict: expected 'approve', got 'block'" in result.diff()
    assert result.to_json()["passed"] is False


async def test_runner_exception_becomes_an_error_result(
    all_cases: list[GoldenCase], session: AsyncSession, settings: Settings
) -> None:
    base = case_by_id(all_cases, "fixture_dirty_four_flags", "A7")
    broken = dataclasses.replace(base, input={**base.input, "comps": [{"comp_id": "x"}]})
    result = await run_case(broken, RunnerContext(session=session, settings=settings))
    assert result.error and "KeyError" in result.error
    assert not result.passed and result.score == 0.0
    unknown = dataclasses.replace(base, task="draft", agent="A4")
    result = await run_case(unknown, RunnerContext(session=session, settings=settings))
    assert result.error == "no runner for A4/draft"


def test_qa_input_from_case_builds_the_a5_contract(all_cases: list[GoldenCase]) -> None:
    case = case_by_id(all_cases, "cadence_within_24h", "A5")
    qa = qa_input_from_case(case)
    assert qa.draft.to == ["jane@acme.com"] and qa.thread_participant == "jane@acme.com"
    assert qa.last_outbound_at is not None and qa.now is not None
    assert (qa.now - qa.last_outbound_at).total_seconds() == 6 * 3600
    assert qa.thread[-1].is_latest_inbound and qa.thread[0].direction == "out"


async def test_recording_provider_records_every_request(settings: Settings) -> None:
    from wholesaling.agents.llm.models import Message, ModelRequest, RequestMetadata

    inner = MockModelProvider()
    recording = RecordingProvider(inner)
    assert recording.name == "mock"
    request = ModelRequest(
        system="s",
        messages=(Message(role="user", content="hi"),),
        metadata=RequestMetadata(agent=AgentId.A5, task_kind=TaskKind.QA),
    )
    await recording.complete(request)
    assert recording.requests == [request] and inner.requests == [request]
    assert recording.requests_for(TaskKind.QA) == [request]
    assert (await recording.healthcheck()).ok


def test_live_mode_is_unavailable_without_a_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not live_available(s)
    with pytest.raises(EvalUnavailable, match="ANTHROPIC_API_KEY"):
        RunnerContext(session=None, settings=s, mode="anthropic").provider_for(  # type: ignore[arg-type]
            GoldenCase(id="x", agent="A5", task="qa", input={"a": 1}, expected={"b": 1})
        )


async def test_run_all_in_both_scripted_modes_passes_every_committed_case(
    all_cases: list[GoldenCase], settings: Settings
) -> None:
    for source in ("case", "expected"):
        results = await run_all(all_cases, settings=settings, script_source=source)  # type: ignore[arg-type]
        failed = [r.diff() for r in results if not r.passed]
        assert not failed, f"{source}: " + "\n".join(failed)
        assert len(results) == len(all_cases)


async def test_run_all_live_without_key_raises_before_running(
    all_cases: list[GoldenCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(EvalUnavailable):
        await run_all(all_cases[:1], settings=s, mode="anthropic")
