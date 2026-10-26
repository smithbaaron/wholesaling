"""``BuyBoxSearch`` (REQUIREMENTS.md WF-07, BB-05, BB-06, OPN-09).

Started by D1 (``activities/deals.run_d1``) once per *new* search-profile version: workflow id
``buy-box-search:{buy_box_id}:v{profile_version}``, input ``BuyBoxSearchInput(search_profile_id,
buy_box_id, profile_version)``. No sleeps: it runs D2 once for its profile version and ends.

Signal ``superseded(SupersededSignal | None)``: a newer profile version exists. Received before
D2 runs it skips the search (the newer version's own workflow searches); received during or after
it is recorded in the status and the result is marked superseded so the review surface can show
which version a match came from. The activity itself also refuses to search a profile that is no
longer active, so a late start cannot write matches for a stale version.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.deals import RunD2Input, RunD2Result, run_d2

WORKFLOW_NAME = "BuyBoxSearch"
WORKFLOW_ID_PREFIX = "buy-box-search:"
ACTIVITY_TIMEOUT = timedelta(minutes=10)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

STATE_PENDING = "pending"
STATE_SEARCHING = "searching"
STATE_DONE = "done"
STATE_SUPERSEDED = "superseded"


def buy_box_search_workflow_id(buy_box_id: str, profile_version: int) -> str:
    return f"{WORKFLOW_ID_PREFIX}{buy_box_id}:v{profile_version}"


@dataclass(slots=True)
class BuyBoxSearchInput:
    search_profile_id: str
    buy_box_id: str
    profile_version: int


@dataclass(slots=True)
class SupersededSignal:
    new_version: int
    new_search_profile_id: str | None = None


@dataclass(slots=True)
class BuyBoxSearchResult:
    search_profile_id: str
    profile_version: int
    searched: bool
    superseded: bool
    superseded_by_version: int | None
    matches_written: int
    match_ids: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


@workflow.defn(name=WORKFLOW_NAME)
class BuyBoxSearch:
    def __init__(self) -> None:
        self._input: BuyBoxSearchInput | None = None
        self._state = STATE_PENDING
        self._superseded_by: int | None = None
        self._result: RunD2Result | None = None

    @workflow.run
    async def run(self, input: BuyBoxSearchInput) -> BuyBoxSearchResult:
        self._input = input
        if self._superseded_by is not None:
            self._state = STATE_SUPERSEDED
            return BuyBoxSearchResult(
                search_profile_id=input.search_profile_id,
                profile_version=input.profile_version,
                searched=False,
                superseded=True,
                superseded_by_version=self._superseded_by,
                matches_written=0,
                skipped_reason="superseded before search",
            )
        self._state = STATE_SEARCHING
        result: RunD2Result = await workflow.execute_activity(
            run_d2,
            RunD2Input(search_profile_id=input.search_profile_id),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        self._result = result
        self._state = STATE_SUPERSEDED if self._superseded_by is not None else STATE_DONE
        return BuyBoxSearchResult(
            search_profile_id=input.search_profile_id,
            profile_version=input.profile_version,
            searched=result.skipped_reason is None,
            superseded=self._superseded_by is not None,
            superseded_by_version=self._superseded_by,
            matches_written=result.matches_written,
            match_ids=list(result.match_ids),
            skipped_reason=result.skipped_reason,
        )

    @workflow.signal
    def superseded(self, payload: SupersededSignal | None = None) -> None:
        version = payload.new_version if payload is not None else -1
        if self._superseded_by is None or version > self._superseded_by:
            self._superseded_by = version

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "search_profile_id": self._input.search_profile_id if self._input else None,
            "profile_version": self._input.profile_version if self._input else None,
            "state": self._state,
            "superseded_by_version": self._superseded_by,
            "result": asdict(self._result) if self._result else None,
        }


__all__ = [
    "ACTIVITY_RETRY",
    "ACTIVITY_TIMEOUT",
    "WORKFLOW_ID_PREFIX",
    "WORKFLOW_NAME",
    "BuyBoxSearch",
    "BuyBoxSearchInput",
    "BuyBoxSearchResult",
    "SupersededSignal",
    "buy_box_search_workflow_id",
]
