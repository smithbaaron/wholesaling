"""BaseAgent builds correct requests; PromptTemplate enforces XC-06."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.agents.llm.conftest import HAIKU, SONNET
from wholesaling.agents.base import (
    BaseAgent,
    CredentialInPromptError,
    PromptTemplate,
    classification_schema,
)
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import Message, TaskKind, prompt_hash
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.ops import AgentRun


class ReplyAgent(BaseAgent):
    agent_id = AgentId.A4
    PROMPT_VERSION = "2026-10-25.1"
    SYSTEM_PROMPT = PromptTemplate("You triage investor replies for {company}. Be brief.")


class ReplyAgentV2(ReplyAgent):
    PROMPT_VERSION = "2026-10-25.2"


def test_prompt_template_rejects_credential_placeholders() -> None:
    for bad in ("{api_key}", "{graph_client_secret}", "{bearer_token}", "{auth}", "{password}"):
        with pytest.raises(CredentialInPromptError):
            PromptTemplate(f"Use {bad} to connect")
    PromptTemplate("Written by {author} for {authority_name}")  # whole-word check: fine


def test_prompt_template_rejects_credential_looking_values_and_literal_secrets() -> None:
    template = PromptTemplate("Company: {company}")
    with pytest.raises(CredentialInPromptError):
        template.render(company="sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(CredentialInPromptError):
        template.render(company="x", api_token="abc")
    with pytest.raises(CredentialInPromptError):
        PromptTemplate("Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(CredentialInPromptError):
        PromptTemplate("-----BEGIN RSA PRIVATE KEY-----")


def test_prompt_template_render_and_missing_values() -> None:
    template = PromptTemplate("Hello {name}, re {subject}")
    assert template.placeholders == ("name", "subject")
    assert template.render(name="Jo", subject="Dallas") == "Hello Jo, re Dallas"
    with pytest.raises(KeyError):
        template.render(name="Jo")


def test_build_request_carries_system_prompt_version_and_schema(
    runner: AgentRunner, session: AsyncSession
) -> None:
    agent = ReplyAgent(runner, session, prompt_values={"company": "NorthStack"})
    request = agent.build_request(
        TaskKind.CLASSIFY,
        "Reply text",
        json_schema=classification_schema(["a", "b"]),
        history=[Message("user", "earlier"), Message("assistant", "ok")],
    )
    assert request.system == "You triage investor replies for NorthStack. Be brief."
    assert request.metadata.agent == AgentId.A4
    assert request.metadata.task_kind == TaskKind.CLASSIFY
    assert request.metadata.prompt_version == "2026-10-25.1"
    assert [m.role for m in request.messages] == ["user", "assistant", "user"]
    assert request.messages[-1].content == "Reply text"
    assert request.json_schema is not None and request.json_schema["properties"]["label"][
        "enum"
    ] == [
        "a",
        "b",
    ]
    assert request.max_tokens == ReplyAgent.DEFAULT_MAX_TOKENS


def test_prompt_version_changes_hash_only(runner: AgentRunner, session: AsyncSession) -> None:
    values = {"company": "NorthStack"}
    r1 = ReplyAgent(runner, session, prompt_values=values).build_request(TaskKind.DRAFT, "x")
    r2 = ReplyAgentV2(runner, session, prompt_values=values).build_request(TaskKind.DRAFT, "x")
    assert r1.system == r2.system
    assert prompt_hash(r1) != prompt_hash(r2)
    assert prompt_hash(r1) == prompt_hash(
        ReplyAgent(runner, session, prompt_values=values).build_request(TaskKind.DRAFT, "x")
    )


def test_agent_without_agent_id_is_rejected(runner: AgentRunner, session: AsyncSession) -> None:
    class Nameless(BaseAgent):
        SYSTEM_PROMPT = "x"

    with pytest.raises(TypeError, match="agent_id"):
        Nameless(runner, session)


async def test_classify_routes_to_haiku_and_returns_label(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    agent = ReplyAgent(runner, session, correlation_id="wf-9", prompt_values={"company": "NS"})
    result = await agent.classify("Sounds good, send me deals", ["interested", "not_interested"])
    # Unscripted: the mock fills the schema, picking the first enum member.
    assert result.label == "interested"
    assert result.confidence == 0.0
    assert provider.requests[0].model == HAIKU
    row = (await session.execute(select(AgentRun))).scalar_one()
    assert row.id == result.run_id and row.correlation_id == "wf-9"
    assert row.output["label"] == "interested"


async def test_classify_rejects_label_outside_set(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed={"label": "maybe", "confidence": 2})
    agent = ReplyAgent(runner, session, prompt_values={"company": "NS"})
    with pytest.raises(StructuredOutputError, match="maybe"):
        await agent.classify("hmm", ["yes", "no"])


async def test_extract_default_and_qa_routing(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    agent = ReplyAgent(runner, session, prompt_values={"company": "NS"})
    schema = {
        "type": "object",
        "properties": {"min_price": {"type": "integer"}},
        "required": ["min_price"],
    }
    parsed, _ = await agent.extract(
        schema, "Buy box: 100k-200k", instructions="Extract the buy box."
    )
    assert parsed == {"min_price": 0}
    assert provider.requests[0].model == HAIKU
    assert provider.requests[0].messages[-1].content.startswith("Extract the buy box.")

    verdict, _ = await agent.extract(
        {"type": "object", "properties": {"verdict": {"enum": ["approve", "block"]}}},
        "draft",
        task_kind=TaskKind.QA,
    )
    assert verdict == {"verdict": "approve"}
    assert provider.requests[1].model == SONNET


async def test_draft_routes_to_sonnet_and_records_decision(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    provider.script(AgentId.A4, TaskKind.DRAFT, text="Thanks for the reply.")
    agent = ReplyAgent(runner, session, prompt_values={"company": "NS"})
    text, run_id = await agent.draft("Reply to: ...", decision="queue_for_human")
    assert text == "Thanks for the reply."
    assert provider.requests[0].model == SONNET
    row = (await session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    assert row.decision == "queue_for_human"


def test_as_json_is_stable() -> None:
    assert BaseAgent.as_json({"b": 1, "a": 2}) == '{\n  "a": 2,\n  "b": 1\n}'
