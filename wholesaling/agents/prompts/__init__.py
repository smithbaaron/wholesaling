"""Prompt constants for the conversation agents (A4, A5).

Prompts are module constants, one module per agent, each with its own ``PROMPT_VERSION``. The
version is hashed into ``agent_runs.prompt_hash`` (docs/agents/llm.md), so bump it on *any*
wording change; the eval harness (BO-13) groups runs by it.
"""

from __future__ import annotations

from wholesaling.agents.prompts import a4, a5

__all__ = ["a4", "a5"]
