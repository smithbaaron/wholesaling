"""Shared fixtures for the eval-harness tests: settings without a .env, the committed cases."""

from __future__ import annotations

import pytest

from wholesaling.evals.cases import GoldenCase, load_all_cases


@pytest.fixture(scope="session")
def all_cases() -> list[GoldenCase]:
    return load_all_cases()


def case_by_id(cases: list[GoldenCase], case_id: str, agent: str | None = None) -> GoldenCase:
    for c in cases:
        if c.id == case_id and (agent is None or c.agent == agent):
            return c
    msg = f"no case {case_id!r}"
    raise KeyError(msg)
