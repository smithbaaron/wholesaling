"""Synthetic investor pilot (REQUIREMENTS.md BO-09, BO-20).

Runs 10 to 20 fake investors through the whole investor loop in mock mode: M1 -> A1 -> A2 ->
A3 ``InvestorSequenceWorkflow`` -> scripted inbound replies through the mock Graph gateway ->
``GraphInboundMessageWorkflow`` (A4 classify, buy-box upsert, A4 draft, A5 QA, policy) -> the
review queue's approve route -> sends in the same conversation. Every scenario declares what the
database must look like afterwards; the report grades them and computes the POL-04 numbers.

Two drivers share the scenario catalogue and the report:

- ``runner.PilotRunner`` (default): the workflows' ``run()`` loops are driven by an in-process
  discrete-event simulator (``sim.py``) that executes the *real* activities and the *real*
  policy / repository / route code, with no Temporal server.
- ``temporal_driver.TemporalPilotRunner`` (``--temporal``): the same scenarios through the
  Temporal time-skipping test environment with a real worker, where the binary can be
  downloaded.

Nothing here reaches a real mailbox or a real model (RUL-02, RUL-06).

``PilotRunner`` is imported lazily (``from wholesaling.pilot.runner import PilotRunner``) because
the runner builds the FastAPI app, whose ``/pilot/report`` route imports this package's report
model.
"""

from __future__ import annotations

from typing import Any

from wholesaling.pilot.report import PilotReport, ScenarioResult
from wholesaling.pilot.scenarios import SCENARIOS, Scenario

__all__ = ["SCENARIOS", "PilotReport", "PilotRunner", "Scenario", "ScenarioResult"]


def __getattr__(name: str) -> Any:
    if name == "PilotRunner":
        from wholesaling.pilot.runner import PilotRunner

        return PilotRunner
    raise AttributeError(name)
