"""BuyBoxSearch run loop without a Temporal server (WF-07): one D2 run per profile version;
``superseded`` before the run skips it, during/after the run is recorded."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from wholesaling.activities import deals as dacts
from wholesaling.workflows import buy_box_search as wbs
from wholesaling.workflows.buy_box_search import BuyBoxSearch, BuyBoxSearchInput, SupersededSignal


@dataclass
class FakeWorkflowModule:
    wf: BuyBoxSearch
    result: dacts.RunD2Result
    on_activity: list[Any] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sim"))

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        self.calls.append((fn.__name__, arg))
        for action in self.on_activity:
            action()
        return self.result


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _make(result: dacts.RunD2Result | None = None) -> FakeWorkflowModule:
        wf = BuyBoxSearch()
        fake = FakeWorkflowModule(
            wf=wf, result=result or dacts.RunD2Result("sp-1", 8, 1, 3, ["m1", "m2", "m3"])
        )
        monkeypatch.setattr(wbs, "workflow", fake)
        return fake

    return _make


async def test_runs_d2_once_for_its_profile_version(sim: Any) -> None:
    fake = sim()
    result = await fake.wf.run(BuyBoxSearchInput("sp-1", "bb-1", 1))
    assert result.searched and not result.superseded and result.matches_written == 3
    assert result.match_ids == ["m1", "m2", "m3"]
    assert [c[0] for c in fake.calls] == ["run_d2"] and fake.calls[0][1].search_profile_id == "sp-1"
    assert fake.wf.status()["state"] == "done"


async def test_superseded_before_start_skips_the_search(sim: Any) -> None:
    fake = sim()
    fake.wf.superseded(SupersededSignal(new_version=2, new_search_profile_id="sp-2"))
    result = await fake.wf.run(BuyBoxSearchInput("sp-1", "bb-1", 1))
    assert not result.searched and result.superseded and result.superseded_by_version == 2
    assert result.skipped_reason == "superseded before search" and fake.calls == []


async def test_superseded_during_the_search_is_recorded(sim: Any) -> None:
    fake = sim()
    fake.on_activity.append(lambda: fake.wf.superseded(SupersededSignal(new_version=3)))
    result = await fake.wf.run(BuyBoxSearchInput("sp-1", "bb-1", 1))
    assert result.searched and result.superseded and result.superseded_by_version == 3
    assert fake.wf.status()["state"] == "superseded"


async def test_activity_refusing_a_stale_profile_shows_as_not_searched(sim: Any) -> None:
    fake = sim(dacts.RunD2Result("sp-1", 0, 0, 0, skipped_reason="profile is superseded"))
    result = await fake.wf.run(BuyBoxSearchInput("sp-1", "bb-1", 1))
    assert not result.searched and result.skipped_reason == "profile is superseded"


async def test_superseded_without_payload_and_highest_version_wins(sim: Any) -> None:
    fake = sim()
    fake.wf.superseded(None)
    fake.wf.superseded(SupersededSignal(new_version=5))
    fake.wf.superseded(SupersededSignal(new_version=4))
    assert fake.wf.status()["superseded_by_version"] == 5
