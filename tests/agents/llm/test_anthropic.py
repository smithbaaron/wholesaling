"""AnthropicProvider against a fake SDK client: structured output, retries, breaker."""

from __future__ import annotations

import dataclasses
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from tests.agents.llm.conftest import SIMPLE_SCHEMA, make_request
from wholesaling.agents.llm.anthropic import (
    STRUCTURED_TOOL_NAME,
    AnthropicProvider,
    extract_json_object,
)
from wholesaling.agents.llm.breaker import BreakerState, CircuitBreaker
from wholesaling.agents.llm.interface import (
    CircuitOpen,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    StructuredOutputError,
)
from wholesaling.agents.llm.models import TaskKind, ToolSpec
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId

MODEL = "claude-haiku-5-5"


def _message(
    *blocks: Any, stop_reason: str = "end_turn", in_tok: int = 10, out_tok: int = 5
) -> Message:
    return Message.model_construct(
        id="msg_1",
        type="message",
        role="assistant",
        model=MODEL,
        content=list(blocks),
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage.model_construct(input_tokens=in_tok, output_tokens=out_tok),
    )


def _text(text: str) -> TextBlock:
    return TextBlock.model_construct(type="text", text=text)


def _tool_use(name: str, data: dict[str, Any], block_id: str = "tu_1") -> ToolUseBlock:
    return ToolUseBlock.model_construct(type="tool_use", id=block_id, name=name, input=data)


def _status_error(status: int, retry_after: str | None = None) -> anthropic.APIStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(
        status, headers=headers, request=httpx.Request("POST", "https://api.anthropic.com")
    )
    cls: type[anthropic.APIStatusError]
    if status == 429:
        cls = anthropic.RateLimitError
    elif status == 529:
        cls = anthropic.OverloadedError
    elif status >= 500:
        cls = anthropic.InternalServerError
    else:
        cls = anthropic.BadRequestError
    return cls(f"http {status}", response=response, body={"error": {"message": f"http {status}"}})


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[no-any-return]


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.messages = FakeMessages(outcomes)


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_provider(
    settings: Settings,
    outcomes: list[Any],
    *,
    breaker: CircuitBreaker | None = None,
    max_attempts: int = 4,
) -> tuple[AnthropicProvider, FakeClient, Sleeper]:
    client = FakeClient(outcomes)
    sleeper = Sleeper()
    provider = AnthropicProvider(
        settings,
        client=client,  # type: ignore[arg-type]
        breaker=breaker,
        max_attempts=max_attempts,
        sleep=sleeper,
    )
    return provider, client, sleeper


def test_requires_api_key_when_no_client_injected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(settings)


def test_builds_client_from_secret_without_leaking_it(settings: Settings) -> None:
    s = settings.model_copy(
        update={"anthropic_api_key": __import__("pydantic").SecretStr("sk-ant-x")}
    )
    provider = AnthropicProvider(s)
    assert isinstance(provider._client, anthropic.AsyncAnthropic)
    assert "sk-ant-x" not in repr(s)


async def test_structured_output_uses_forced_tool_and_parses_input(settings: Settings) -> None:
    provider, client, _ = make_provider(
        settings,
        [
            _message(
                _tool_use(STRUCTURED_TOOL_NAME, {"label": "interested"}), stop_reason="tool_use"
            )
        ],
    )
    request = make_request(json_schema=SIMPLE_SCHEMA, model=MODEL)
    response = await provider.complete(request)

    sent = client.messages.calls[0]
    assert sent["model"] == MODEL
    assert sent["tool_choice"] == {"type": "tool", "name": STRUCTURED_TOOL_NAME}
    assert sent["tools"][-1]["input_schema"] is SIMPLE_SCHEMA
    assert sent["system"] == request.system
    assert sent["messages"] == [{"role": "user", "content": "Hello"}]
    assert "temperature" not in sent
    assert response.parsed == {"label": "interested"}
    assert response.tool_calls == ()  # the structured tool is not surfaced as a tool call
    assert response.input_tokens == 10 and response.output_tokens == 5
    assert response.model == MODEL and response.stop_reason == "tool_use"
    assert provider.breaker.state is BreakerState.CLOSED


async def test_structured_output_falls_back_to_json_in_text(settings: Settings) -> None:
    provider, _, _ = make_provider(
        settings, [_message(_text('Sure:\n```json\n{"label": "not_interested"}\n```'))]
    )
    response = await provider.complete(make_request(json_schema=SIMPLE_SCHEMA, model=MODEL))
    assert response.parsed == {"label": "not_interested"}


async def test_structured_output_missing_raises(settings: Settings) -> None:
    provider, _, _ = make_provider(settings, [_message(_text("I cannot do that."))])
    with pytest.raises(StructuredOutputError):
        await provider.complete(make_request(json_schema=SIMPLE_SCHEMA, model=MODEL))


async def test_plain_text_and_user_tools(settings: Settings) -> None:
    tool = ToolSpec("lookup", "Look up a record", {"type": "object", "properties": {}})
    provider, client, _ = make_provider(
        settings, [_message(_text("Let me check."), _tool_use("lookup", {"id": 1}, "tu_9"))]
    )
    request = make_request(AgentId.A3, TaskKind.DRAFT, model=MODEL)
    request = dataclasses.replace(request, tools=(tool,))
    response = await provider.complete(request)
    assert client.messages.calls[0]["tool_choice"] == {"type": "auto"}
    assert response.text == "Let me check."
    assert response.parsed is None
    assert response.tool_calls[0].name == "lookup" and response.tool_calls[0].input == {"id": 1}


async def test_retries_on_429_honouring_retry_after(settings: Settings) -> None:
    provider, client, sleeper = make_provider(
        settings, [_status_error(429, "2"), _status_error(529), _message(_text("ok"))]
    )
    response = await provider.complete(make_request(model=MODEL))
    assert response.text == "ok"
    assert len(client.messages.calls) == 3
    assert sleeper.delays[0] == 2.0  # retry-after header wins
    assert 0.5 <= sleeper.delays[1] <= 2.0  # exponential backoff with jitter on attempt 2
    assert provider.breaker.failures == 0  # success resets


async def test_rate_limited_after_exhausting_attempts(settings: Settings) -> None:
    provider, client, _ = make_provider(settings, [_status_error(429, "1")] * 3, max_attempts=3)
    with pytest.raises(RateLimited) as excinfo:
        await provider.complete(make_request(model=MODEL))
    assert excinfo.value.retry_after_s == 1.0
    assert len(client.messages.calls) == 3


async def test_5xx_after_retries_is_provider_unavailable(settings: Settings) -> None:
    provider, _, _ = make_provider(settings, [_status_error(500)] * 2, max_attempts=2)
    with pytest.raises(ProviderUnavailable):
        await provider.complete(make_request(model=MODEL))


async def test_connection_errors_retry_then_fail(settings: Settings) -> None:
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    provider, client, _ = make_provider(settings, [err, err], max_attempts=2)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        await provider.complete(make_request(model=MODEL))
    assert len(client.messages.calls) == 2


async def test_4xx_is_not_retried_and_does_not_trip_breaker(settings: Settings) -> None:
    provider, client, _ = make_provider(settings, [_status_error(400)])
    with pytest.raises(InvalidRequest):
        await provider.complete(make_request(model=MODEL))
    assert len(client.messages.calls) == 1
    assert provider.breaker.failures == 0


async def test_circuit_opens_after_n_failures_and_blocks_calls(settings: Settings) -> None:
    breaker = CircuitBreaker(name="anthropic", failure_threshold=3, open_seconds=60)
    provider, client, _ = make_provider(
        settings, [_status_error(500)] * 10, breaker=breaker, max_attempts=10
    )
    with pytest.raises(ProviderUnavailable):
        await provider.complete(make_request(model=MODEL))
    # Stopped retrying as soon as the breaker opened: 3 calls, not 10.
    assert len(client.messages.calls) == 3
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpen):
        await provider.complete(make_request(model=MODEL))
    assert len(client.messages.calls) == 3  # not called while open


async def test_unrouted_request_is_rejected(settings: Settings) -> None:
    provider, _, _ = make_provider(settings, [])
    with pytest.raises(InvalidRequest, match="model"):
        await provider.complete(make_request())


async def test_healthcheck_reports_open_breaker(settings: Settings) -> None:
    breaker = CircuitBreaker(name="anthropic", failure_threshold=1)
    breaker.record_failure()
    provider, _, _ = make_provider(settings, [], breaker=breaker)
    status = await provider.healthcheck()
    assert status.ok is False and "open" in status.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('prefix {"a": {"b": 2}} suffix', {"a": {"b": 2}}),
        ('```json\n{"a": 3}\n```', {"a": 3}),
        ("[1, 2]", None),
        ("no json here", None),
    ],
)
def test_extract_json_object(text: str, expected: dict[str, Any] | None) -> None:
    assert extract_json_object(text) == expected
