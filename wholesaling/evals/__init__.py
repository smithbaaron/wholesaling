"""Agent eval harness (REQUIREMENTS.md BO-13, INV-22): golden test sets per model-backed agent.

Modules:

- ``cases``    one golden-case schema, a loader that validates every committed JSON file and
               migration shims for the two older per-agent formats
- ``scoring``  exact-match, set-F1, numeric tolerance, text contains / excludes and
               never-invent checks, each producing a ``Check`` with a diff
- ``runners``  one runner per agent (A4 classify / extract, A5 QA, S4 intake, A7 V3 comp
               audit) that drives the agent's deterministic path end to end with the model
               stubbed from the case (``scripted``) or live (``anthropic``)
- ``report``   per-agent pass rates, findings, the baseline regression gate
- ``cli``      ``wholesaling-evals run [--agent A4] [--provider mock|anthropic] [--out ...]``

Nothing here changes an agent: a golden case the agent fails is a finding in the report.
"""

from wholesaling.evals.cases import GoldenCase, GoldenSchemaError, load_all_cases, load_file
from wholesaling.evals.report import EvalReport, RegressionResult, compare_to_baseline

__all__ = [
    "EvalReport",
    "GoldenCase",
    "GoldenSchemaError",
    "RegressionResult",
    "compare_to_baseline",
    "load_all_cases",
    "load_file",
]
