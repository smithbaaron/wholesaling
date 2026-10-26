"""``wholesaling-pilot`` argument handling and ``GET /pilot/report``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.api.conftest import AUTH, operator_settings
from wholesaling.api.main import create_app
from wholesaling.api.routes.pilot import PILOT_REPORT_EVENT, PILOT_REPORT_PATH_ENV
from wholesaling.db import Base, make_session_factory
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.pilot import cli
from wholesaling.pilot.report import Check, PilotReport, ScenarioResult
from wholesaling.policy.actions import AutonomyLevel, PolicyAction


def _report(ok: bool = True) -> PilotReport:
    return PilotReport(
        mode="sim",
        level="L0",
        approve_everything=True,
        seed=1,
        markets=["Houston"],
        started_at="2026-10-25T00:00:00+00:00",
        finished_at="2026-10-25T00:00:05+00:00",
        wall_seconds=5.0,
        sim_start="2026-10-27T15:00:00+00:00",
        sim_end="2026-11-10T15:00:00+00:00",
        simulated_days=14.0,
        database_url="sqlite+aiosqlite:///:memory:",
        scenarios=[
            ScenarioResult(
                scenario_id="s",
                title="t",
                market="Houston",
                investor="Acme",
                status="pass" if ok else "fail",
                checks=[Check("touches_sent", 4, 4 if ok else 3, ok)],
            )
        ],
    )


# -- CLI ------------------------------------------------------------------------------------------


def test_parser_defaults_and_options() -> None:
    args = cli.build_parser().parse_args(["run"])
    assert args.markets is None and args.level == "L0" and not args.temporal
    args = cli.build_parser().parse_args(
        ["run", "--market", "Houston", "--market", "Columbus", "--level", "L1", "--strict"]
    )
    assert args.markets == ["Houston", "Columbus"] and args.level == "L1" and args.strict


def test_level_overrides_cover_the_three_investor_actions() -> None:
    assert cli.level_overrides("L0") == {}
    assert cli.level_overrides("L1") == {
        PolicyAction.INVESTOR_INITIAL_OUTREACH: AutonomyLevel.L1,
        PolicyAction.INVESTOR_FOLLOWUP: AutonomyLevel.L1,
        PolicyAction.INVESTOR_REPLY: AutonomyLevel.L1,
    }


def test_main_writes_reports_and_exit_code_follows_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = iter([_report(ok=True), _report(ok=False)])

    async def fake_run(_args: Any) -> PilotReport:
        return next(outcomes)

    monkeypatch.setattr(cli, "_run", fake_run)
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    assert cli.main(["run", "--out", str(md), "--json", str(js), "--quiet"]) == 0
    assert md.read_text().startswith("# Synthetic investor pilot")
    assert json.loads(js.read_text())["ok"] is True
    assert "PASS: 1 passed" in capsys.readouterr().out
    assert cli.main(["run"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_strict_turns_known_defects_into_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    report.scenarios[0].status = "known_defect"
    report.scenarios[0].known_defects = ["GAP-02"]

    async def fake_run(_args: Any) -> PilotReport:
        return report

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["run", "--quiet"]) == 0
    assert cli.main(["run", "--quiet", "--strict"]) == 1


def test_main_exits_2_when_the_temporal_server_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(_args: Any) -> PilotReport:
        raise RuntimeError("Temporal time-skipping test server unavailable")

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["run", "--temporal"]) == 2


# -- route ----------------------------------------------------------------------------------------


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    import wholesaling.db.models  # noqa: F401

    application = create_app(operator_settings())
    async with application.router.lifespan_context(application):
        async with application.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", auth=AUTH
    ) as c:
        yield c


async def test_report_route_requires_the_operator(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/pilot/report")).status_code == 401


async def test_report_route_404_without_a_report(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(PILOT_REPORT_PATH_ENV, str(tmp_path / "missing.json"))
    response = await client.get("/pilot/report")
    assert response.status_code == 404 and "wholesaling-pilot run" in response.json()["detail"]


async def test_report_route_serves_the_file_in_three_formats(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "pilot-report.json"
    path.write_text(_report().to_json())
    monkeypatch.setenv(PILOT_REPORT_PATH_ENV, str(path))
    as_json = await client.get("/pilot/report")
    assert as_json.status_code == 200 and as_json.json()["ok"] is True
    as_text = await client.get("/pilot/report", params={"format": "text"})
    assert "SYNTHETIC INVESTOR PILOT" in as_text.text
    as_md = await client.get("/pilot/report", params={"format": "markdown"})
    assert (
        as_md.text.startswith("# Synthetic investor pilot")
        and "markdown" in as_md.headers["content-type"]
    )
    assert (await client.get("/pilot/report", params={"format": "pdf"})).status_code == 422


async def test_report_route_prefers_the_database_record(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(PILOT_REPORT_PATH_ENV, str(tmp_path / "missing.json"))
    stored = _report()
    stored.seed = 777
    async with make_session_factory(app.state.engine)() as s:
        await AuditRepository(s).record(
            actor=Actor.SYSTEM,
            actor_id="pilot",
            event_type=PILOT_REPORT_EVENT,
            entity_type="pilot",
            payload=stored.to_dict(),
        )
        await s.commit()
    response = await client.get("/pilot/report")
    assert response.status_code == 200 and response.json()["seed"] == 777
