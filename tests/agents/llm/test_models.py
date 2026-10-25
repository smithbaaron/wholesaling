"""ModelRequest validation and the prompt hash contract (XC-02)."""

from __future__ import annotations

import dataclasses

import pytest

from tests.agents.llm.conftest import SIMPLE_SCHEMA, make_request
from wholesaling.agents.llm.models import (
    Message,
    ModelRequest,
    RequestMetadata,
    TaskKind,
    prompt_hash,
)
from wholesaling.db.models.enums import AgentId


def test_request_requires_messages_and_positive_max_tokens() -> None:
    meta = RequestMetadata(agent=AgentId.A1, task_kind=TaskKind.EXTRACT)
    with pytest.raises(ValueError, match="at least one message"):
        ModelRequest(system="s", messages=(), metadata=meta)
    with pytest.raises(ValueError, match="max_tokens"):
        ModelRequest(system="s", messages=(Message("user", "x"),), metadata=meta, max_tokens=0)


def test_prompt_hash_is_stable_sha256_hex() -> None:
    a = prompt_hash(make_request())
    b = prompt_hash(make_request())
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_prompt_hash_changes_with_prompt_version() -> None:
    assert prompt_hash(make_request(prompt_version="1")) != prompt_hash(
        make_request(prompt_version="2")
    )


def test_prompt_hash_changes_with_system_messages_and_schema() -> None:
    base = prompt_hash(make_request())
    assert prompt_hash(make_request(system="other")) != base
    assert prompt_hash(make_request(user="other")) != base
    assert prompt_hash(make_request(json_schema=SIMPLE_SCHEMA)) != base


def test_prompt_hash_ignores_model_and_max_tokens() -> None:
    req = make_request()
    assert prompt_hash(dataclasses.replace(req, model="claude-opus-5-5")) == prompt_hash(req)
    assert prompt_hash(dataclasses.replace(req, max_tokens=99)) == prompt_hash(req)


def test_hash_material_is_json_canonical_regardless_of_schema_key_order() -> None:
    s1 = {"type": "object", "properties": {"a": {"type": "string"}}}
    s2 = {"properties": {"a": {"type": "string"}}, "type": "object"}
    assert prompt_hash(make_request(json_schema=s1)) == prompt_hash(make_request(json_schema=s2))
