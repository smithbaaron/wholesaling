"""``BuyBoxEventWorkflow`` (REQUIREMENTS.md WF-20, INV-21): what happens after a buy box version
is written.

Started per ``buy_box.created`` / ``buy_box.updated`` audit event (emitted by
``activities.conversation.extract_and_upsert_buy_box``). The workflow id is derived from
``(buy_box_id, version)`` so the same event delivered twice starts one run (XC-03).

Steps, in order:

1. **Sheets** — nothing to do here. The Buy Boxes tab is refreshed by the next
   ``SheetsSyncWorkflow`` fire (15 min, WF-13); the projection reads Postgres, so no push is
   needed and no agent ever writes to Sheets (STK-02).
2. **D1 -> D2** (BO-11, not built yet) — ``_run_search_steps`` is the extension point: BO-11
   replaces its body with the D1 search-profile activity and the D2 match activity and fills
   ``BuyBoxEventResult.search_profile_id`` / ``matches``. It is a separate method so the parser
   step below and the workflow id contract do not move when D1 lands.
3. **A6 -> Zapier parser** (INV-20, RUL-07) — always invoked; the activity itself no-ops with an
   audit note when ``ZAPIER_PARSER_ENABLED`` is false and is idempotent per (buy box, version).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.zapier import (
        SendParserEmailInput,
        SendParserEmailResult,
        send_parser_email,
    )

PARSER_ACTIVITY_TIMEOUT = timedelta(minutes=2)
PARSER_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
EVENT_TYPES = frozenset({"buy_box.created", "buy_box.updated"})


def buy_box_event_workflow_id(buy_box_id: str, version: int) -> str:
    return f"buy-box-event-{buy_box_id}-v{version}"


@dataclass(slots=True)
class BuyBoxEventInput:
    buy_box_id: str
    investor_id: str
    version: int
    event_type: str = "buy_box.updated"  # buy_box.created | buy_box.updated
    buy_box_key: str = "default"
    source_message_id: str | None = None


@dataclass(slots=True)
class BuyBoxEventResult:
    buy_box_id: str
    version: int
    event_type: str
    # Step 2 (BO-11 extension point): filled once D1 / D2 exist.
    search_profile_id: str | None = None
    matches: list[str] = field(default_factory=list)
    # Step 3
    parser: SendParserEmailResult | None = None
    steps: list[str] = field(default_factory=list)


@workflow.defn(name="BuyBoxEventWorkflow")
class BuyBoxEventWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: BuyBoxEventResult | None = None

    @workflow.run
    async def run(self, input: BuyBoxEventInput) -> BuyBoxEventResult:
        if input.event_type not in EVENT_TYPES:
            # Fail the run, do not retry the workflow task: the input is wrong, not the world.
            raise ApplicationError(
                f"unsupported buy box event {input.event_type!r}", non_retryable=True
            )
        result = BuyBoxEventResult(
            buy_box_id=input.buy_box_id, version=input.version, event_type=input.event_type
        )
        # Step 1: Sheets refresh happens on the next scheduled sync; nothing to call.
        result.steps.append("sheets:next_scheduled_sync")

        self._state = "search"
        await self._run_search_steps(input, result)

        self._state = "parser"
        parser: SendParserEmailResult = await workflow.execute_activity(
            send_parser_email,
            SendParserEmailInput(buy_box_id=input.buy_box_id),
            start_to_close_timeout=PARSER_ACTIVITY_TIMEOUT,
            retry_policy=PARSER_RETRY,
        )
        result.parser = parser
        result.steps.append(
            "parser:sent" if parser.sent else f"parser:skipped:{parser.skipped_reason or 'unknown'}"
        )
        self._result = result
        self._state = "done"
        return result

    async def _run_search_steps(self, input: BuyBoxEventInput, result: BuyBoxEventResult) -> None:
        """BO-11 extension point: D1 (search profile) then D2 (match) activities go here.

        Until D1 exists this records that the step was reached and does nothing else. Keep the
        call order (search before parser) so the parser email can later carry the profile id.
        """
        result.steps.append("search:not_built")

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = [
    "EVENT_TYPES",
    "PARSER_RETRY",
    "BuyBoxEventInput",
    "BuyBoxEventResult",
    "BuyBoxEventWorkflow",
    "buy_box_event_workflow_id",
]
