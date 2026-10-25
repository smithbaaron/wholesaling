"""MockModelProvider: schema filling, scripting, token counting, recording."""

from __future__ import annotations

import pytest

from tests.agents.llm.conftest import SIMPLE_SCHEMA, make_request
from wholesaling.agents.llm.interface import ProviderUnavailable
from wholesaling.agents.llm.mock import (
    MockModelProvider,
    ScriptedResponse,
    placeholder_for_schema,
)
from wholesaling.agents.llm.models import ModelRequest, TaskKind
from wholesaling.db.models.enums import AgentId


def test_placeholder_fills_required_keys_with_typed_values() -> None:
    value = placeholder_for_schema(SIMPLE_SCHEMA)
    assert value == {
        "label": "interested",  # first enum member
        "confidence": 0.0,
        "count": 0,
        "flag": False,
        "tags": [],
        "nested": {"city": "mock", "zip": "mock"},  # nullable union -> first non-null type
    }
    assert "optional_note" not in value


def test_placeholder_honours_const_default_minimum_and_min_items() -> None:
    schema = {
        "type": "object",
        "properties": {
            "kind": {"const": "offer"},
            "n": {"type": "integer", "default": 7},
            "score": {"type": "number", "minimum": 0.5},
            "items": {"type": "array", "minItems": 2, "items": {"type": "boolean"}},
            "either": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        },
    }
    assert placeholder_for_schema(schema) == {
        "kind": "offer",
        "n": 7,
        "score": 0.5,
        "items": [False, False],
        "either": 0,
    }


async def test_default_response_with_schema_is_parsed_object(provider: MockModelProvider) -> None:
    response = await provider.complete(make_request(json_schema=SIMPLE_SCHEMA, model="m"))
    assert response.parsed is not None
    assert response.parsed["label"] == "interested"
    assert response.model == "m"
    assert response.text  # JSON text mirrors the object
    assert provider.requests and provider.requests[0].json_schema is SIMPLE_SCHEMA


async def test_default_response_without_schema_echoes(provider: MockModelProvider) -> None:
    response = await provider.complete(make_request(task_kind=TaskKind.DRAFT, user="Write it"))
    assert response.parsed is None
    assert "Write it" in response.text
    assert response.model == "mock-model"


async def test_tokens_counted_as_len_over_four(provider: MockModelProvider) -> None:
    req = make_request(system="a" * 40, user="b" * 40)
    provider.script(AgentId.A4, TaskKind.CLASSIFY, text="c" * 20)
    response = await provider.complete(req)
    assert response.input_tokens == 20
    assert response.output_tokens == 5


async def test_scripted_by_agent_and_task(provider: MockModelProvider) -> None:
    provider.script(AgentId.A5, TaskKind.QA, parsed={"verdict": "block"})
    other = await provider.complete(make_request(AgentId.A4, TaskKind.QA))
    scripted = await provider.complete(make_request(AgentId.A5, TaskKind.QA))
    assert other.parsed is None
    assert scripted.parsed == {"verdict": "block"}


async def test_scripted_text_that_is_json_becomes_parsed_when_schema_given(
    provider: MockModelProvider,
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, text='{"label": "x"}')
    response = await provider.complete(make_request(json_schema=SIMPLE_SCHEMA))
    assert response.parsed == {"label": "x"}


async def test_matcher_wins_over_pair_script(provider: MockModelProvider) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, text="pair")
    provider.script_containing("URGENT", ScriptedResponse(text="matched"))
    assert (await provider.complete(make_request(user="URGENT reply"))).text == "matched"
    assert (await provider.complete(make_request(user="calm reply"))).text == "pair"


async def test_callable_responder_sees_request(provider: MockModelProvider) -> None:
    def responder(request: ModelRequest) -> ScriptedResponse:
        return ScriptedResponse(text=request.messages[-1].content.upper())

    provider.script(AgentId.A3, TaskKind.DRAFT, responder=responder)
    response = await provider.complete(make_request(AgentId.A3, TaskKind.DRAFT, user="hi"))
    assert response.text == "HI"


async def test_scripted_error_and_fail_with(provider: MockModelProvider) -> None:
    provider.script(AgentId.A1, TaskKind.EXTRACT, raise_error=ProviderUnavailable("down"))
    with pytest.raises(ProviderUnavailable):
        await provider.complete(make_request(AgentId.A1, TaskKind.EXTRACT))
    failing = MockModelProvider(fail_with=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await failing.complete(make_request())
    assert len(failing.requests) == 1  # recorded even when failing


async def test_healthcheck(provider: MockModelProvider) -> None:
    status = await provider.healthcheck()
    assert status.ok and status.mode == "mock" and status.name == "mock"
