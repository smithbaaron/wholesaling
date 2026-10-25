"""Anthropic provider over the official ``anthropic`` SDK (REQUIREMENTS.md STK-07 default).

Structured output
    When a request carries ``json_schema`` we define one tool, ``emit_result``, whose
    ``input_schema`` *is* that schema, and force it with ``tool_choice={"type": "tool"}``. The
    model's tool input is the parsed object. This works on every model and needs no beta header.
    If the model answers with text instead, we fall back to the first JSON object we can find in
    the text. If neither yields an object we raise ``StructuredOutputError``.

Resilience (XC-08)
    Retries with exponential backoff + jitter on 429, 529 (overloaded), any 5xx, timeouts and
    connection errors, honouring ``retry-after`` when the API sends one. The SDK's own retries are
    turned off (``max_retries=0``) so this class is the single place that decides. A
    ``CircuitBreaker`` opens after N consecutive failures and fails fast while open.

Logging
    The API key is never logged (``SecretStr`` + the structlog redactor). Prompts and raw
    responses are logged at DEBUG only; INFO carries model, token counts, latency and attempt.

Not supported on purpose
    ``ModelRequest.temperature`` is ignored: current Anthropic models use adaptive thinking and
    the SDK's ``messages.create`` (v2.x) no longer accepts ``temperature``. Other providers may
    honour it.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anthropic
from anthropic.types import (
    Message,
    MessageParam,
    TextBlock,
    ToolChoiceAutoParam,
    ToolChoiceToolParam,
    ToolParam,
    ToolUseBlock,
)

from wholesaling.agents.llm.breaker import CircuitBreaker
from wholesaling.agents.llm.interface import (
    InvalidRequest,
    ModelError,
    ProviderUnavailable,
    RateLimited,
    StructuredOutputError,
)
from wholesaling.agents.llm.models import ModelRequest, ModelResponse, ToolCall
from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus
from wholesaling.logging import get_logger

log = get_logger(__name__)

STRUCTURED_TOOL_NAME = "emit_result"
RETRYABLE_STATUS = frozenset({408, 409, 429, 529})
Sleeper = Callable[[float], Awaitable[None]]


def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or 500 <= status <= 599


def _retry_after_seconds(exc: anthropic.APIStatusError) -> float | None:
    header = exc.response.headers.get("retry-after")
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None  # HTTP-date form: fall back to backoff


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort: a whole-string JSON object, a ```json fenced block, or the outermost braces."""
    candidates: list[str] = [text.strip()]
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    braces = _JSON_OBJECT_RE.search(text)
    if braces:
        candidates.append(braces.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        settings: Settings,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        breaker: CircuitBreaker | None = None,
        max_attempts: int = 4,
        base_delay_s: float = 0.5,
        max_delay_s: float = 20.0,
        timeout_s: float = 120.0,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if client is None:
            if settings.anthropic_api_key is None:
                msg = "MODEL_PROVIDER=anthropic requires ANTHROPIC_API_KEY (see .env.example)"
                raise ValueError(msg)
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                max_retries=0,
                timeout=timeout_s,
            )
        self._client = client
        self.breaker = breaker or CircuitBreaker(name="anthropic")
        self._max_attempts = max(1, max_attempts)
        self._base_delay = base_delay_s
        self._max_delay = max_delay_s
        self._sleep = sleep
        self._rng = rng or random.Random()  # jitter only, not security-relevant

    # -- ModelProvider ---------------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.model is None:
            msg = "request.model must be set; route it through AgentRunner / ModelRouter"
            raise InvalidRequest(msg)
        self.breaker.check()
        kwargs = self._build_kwargs(request)
        log.debug("llm.anthropic.request", model=request.model, body=kwargs)

        started = time.perf_counter()
        message = await self._call_with_retries(kwargs, request.model)
        latency_ms = int((time.perf_counter() - started) * 1000)

        response = self._to_response(message, request, latency_ms)
        log.info(
            "llm.anthropic.completed",
            model=response.model,
            agent=request.metadata.agent.value,
            task_kind=request.metadata.task_kind.value,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
            stop_reason=response.stop_reason,
        )
        log.debug("llm.anthropic.response", raw=message.model_dump(mode="json"))
        return response

    async def healthcheck(self) -> HealthStatus:
        try:
            self.breaker.check()
            await self._client.models.list(limit=1)
        except Exception as exc:  # health must never raise
            return HealthStatus(name=self.name, mode="live", ok=False, detail=str(exc))
        return HealthStatus(name=self.name, mode="live", ok=True)

    # -- request building ------------------------------------------------------------------------

    @staticmethod
    def _build_kwargs(request: ModelRequest) -> dict[str, Any]:
        messages: list[MessageParam] = []
        for m in request.messages:
            if m.role == "user":
                messages.append({"role": "user", "content": m.content})
            else:
                messages.append({"role": "assistant", "content": m.content})
        tools: list[ToolParam] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in request.tools
        ]
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.system:
            kwargs["system"] = request.system
        if request.json_schema is not None:
            tools.append(
                {
                    "name": STRUCTURED_TOOL_NAME,
                    "description": (
                        "Return the final answer as a JSON object that matches the schema. "
                        "Always call this tool exactly once."
                    ),
                    "input_schema": request.json_schema,
                }
            )
            choice: ToolChoiceToolParam = {"type": "tool", "name": STRUCTURED_TOOL_NAME}
            kwargs["tool_choice"] = choice
        elif tools:
            auto: ToolChoiceAutoParam = {"type": "auto"}
            kwargs["tool_choice"] = auto
        if tools:
            kwargs["tools"] = tools
        return kwargs

    # -- retries -----------------------------------------------------------------------------

    async def _call_with_retries(self, kwargs: dict[str, Any], model: str) -> Message:
        attempt = 0
        while True:
            attempt += 1
            try:
                message = await self._client.messages.create(**kwargs)
            except anthropic.APIStatusError as exc:
                if not _is_retryable_status(exc.status_code):
                    # A 4xx we caused: not a provider failure, do not trip the breaker.
                    msg = f"anthropic rejected the request ({exc.status_code}): {exc.message}"
                    raise InvalidRequest(msg) from exc
                self.breaker.record_failure()
                retry_after = _retry_after_seconds(exc)
                if attempt >= self._max_attempts or self.breaker.failures >= (
                    self.breaker.failure_threshold
                ):
                    raise self._exhausted(exc, exc.status_code, retry_after, attempt) from exc
                delay = self._delay(attempt, retry_after)
                log.warning(
                    "llm.anthropic.retry",
                    model=model,
                    status=exc.status_code,
                    attempt=attempt,
                    delay_s=round(delay, 3),
                )
                await self._sleep(delay)
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                self.breaker.record_failure()
                if attempt >= self._max_attempts or self.breaker.failures >= (
                    self.breaker.failure_threshold
                ):
                    msg = f"anthropic unreachable after {attempt} attempt(s): {exc}"
                    raise ProviderUnavailable(msg) from exc
                delay = self._delay(attempt, None)
                log.warning(
                    "llm.anthropic.retry",
                    model=model,
                    status="connection",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                )
                await self._sleep(delay)
            else:
                if not isinstance(message, Message):  # streaming is never requested
                    msg = "unexpected streaming response from anthropic"
                    raise ModelError(msg)
                self.breaker.record_success()
                return message

    @staticmethod
    def _exhausted(
        exc: anthropic.APIStatusError, status: int, retry_after: float | None, attempts: int
    ) -> ModelError:
        if status in (429, 529):
            return RateLimited(
                f"anthropic {status} after {attempts} attempt(s): {exc.message}",
                retry_after_s=retry_after,
            )
        return ProviderUnavailable(f"anthropic {status} after {attempts} attempt(s): {exc.message}")

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._max_delay)
        backoff = self._base_delay * (2 ** (attempt - 1))
        jitter = float(self._rng.uniform(0, backoff / 2))
        return float(min(backoff + jitter, self._max_delay))

    # -- response parsing ----------------------------------------------------------------------

    @staticmethod
    def _to_response(message: Message, request: ModelRequest, latency_ms: int) -> ModelResponse:
        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        parsed: dict[str, Any] | None = None
        for block in _blocks(message):
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_input = dict(block.input) if isinstance(block.input, dict) else {}
                if request.json_schema is not None and block.name == STRUCTURED_TOOL_NAME:
                    parsed = tool_input
                else:
                    tool_calls.append(ToolCall(id=block.id, name=block.name, input=tool_input))
        text = "\n".join(t for t in texts if t)
        if request.json_schema is not None and parsed is None:
            parsed = extract_json_object(text)
            if parsed is None:
                msg = (
                    f"model {message.model} returned no structured output for "
                    f"{request.metadata.agent.value}/{request.metadata.task_kind.value} "
                    f"(stop_reason={message.stop_reason})"
                )
                raise StructuredOutputError(msg)
        usage = message.usage
        return ModelResponse(
            text=text,
            model=message.model,
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            parsed=parsed,
            tool_calls=tuple(tool_calls),
            stop_reason=str(message.stop_reason) if message.stop_reason else None,
            latency_ms=latency_ms,
            raw=message.model_dump(mode="json"),
        )


def _blocks(message: Message) -> Iterable[Any]:
    return message.content or ()
