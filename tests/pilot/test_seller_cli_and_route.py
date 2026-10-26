"""``wholesaling-pilot seller`` argument handling and ``GET /pilot/seller-report``."""

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
from wholesaling.api.routes.pilot import SELLER_REPORT_EVENT, SELLER_REPORT_PATH_ENV
from wholesaling.db import Base, make_session_factory
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.pilot import cli
from wholesaling.pilot.report import Check, ScenarioResult
from wholesaling.pilot.seller_report import SellerPilotReport


def _report(ok: bool = True) -> SellerPilotReport:
    return SellerPilotReport(
        mode="sim",
        level="L0",
        approve_everything=True,
        seed=1,
        market="Houston",
        started_at="2026-10-25T00:00:00+00:00",
        finished_at="2026-10-25T00:00:07+00:00",
        wall_seconds=7.0,
        sim_start="2026-10-27T15:00:00+00:00",
        sim_end="2027-01-27T15:00:00+00:00",
        simulated_days=92.0,
        database_url="sqlite+aiosqlite:///:memory:",
        scenarios=[
            ScenarioResult(
                scenario_id="seed_no_reply",
                title="t",
                market="Houston",
                investor="Quiet Owner",
                status="pass" if ok else "fail",
                checks=[Check("touches_sent", 4, 4 if ok else 3, ok)],
            )
        ],
    )


# -- CLI ------------------------------------------------------------------------------------------


def test_seller_subcommand_defaults_and_options() -> None:
    args = cli.build_parser().parse_args(["seller"])
    assert args.command == "seller" and args.level == "L0" and not args.no_approve
    args = cli.build_parser().parse_args(
        ["seller", "--level", "L1", "--strict", "--seed", "7", "--no-approve", "--quiet"]
    )
    assert args.level == "L1" and args.strict and args.seed == 7 and args.no_approve
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["seller", "--level", "L2"])  # the seller pilot is L0/L1


def test_seller_main_writes_reports_and_exit_code_follows_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = iter([_report(ok=True), _report(ok=False)])
    calls: list[Any] = []

    async def fake_run(args: Any) -> SellerPilotReport:
        calls.append(args)
        return next(outcomes)

    async def never(_args: Any) -> Any:
        raise AssertionError("the investor runner must not run for `seller`")

    monkeypatch.setattr(cli, "_run_seller", fake_run)
    monkeypatch.setattr(cli, "_run", never)
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    assert cli.main(["seller", "--out", str(md), "--json", str(js), "--quiet"]) == 0
    assert md.read_text().startswith("# Synthetic seller pilot")
    assert json.loads(js.read_text())["kind"] == "seller"
    assert "PASS: 1 passed" in capsys.readouterr().out
    assert cli.main(["seller", "--level", "L1"]) == 1
    assert "SYNTHETIC SELLER PILOT" in capsys.readouterr().out
    assert calls[1].level == "L1"


def test_seller_strict_turns_known_defects_into_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    report.scenarios[0].status = "known_defect"
    report.scenarios[0].known_defects = ["GAP-S03"]

    async def fake_run(_args: Any) -> SellerPilotReport:
        return report

    monkeypatch.setattr(cli, "_run_seller", fake_run)
    assert cli.main(["seller", "--quiet"]) == 0
    assert cli.main(["seller", "--quiet", "--strict"]) == 1


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


async def test_seller_report_route_requires_the_operator(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/pilot/seller-report")).status_code == 401


async def test_seller_report_route_404_without_a_report(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(SELLER_REPORT_PATH_ENV, str(tmp_path / "missing.json"))
    response = await client.get("/pilot/seller-report")
    assert response.status_code == 404
    assert "wholesaling-pilot seller" in response.json()["detail"]


async def test_seller_report_route_serves_the_file_in_three_formats(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "seller-pilot-report.json"
    path.write_text(_report().to_json())
    monkeypatch.setenv(SELLER_REPORT_PATH_ENV, str(path))
    as_json = await client.get("/pilot/seller-report")
    assert as_json.status_code == 200 and as_json.json()["kind"] == "seller"
    as_text = await client.get("/pilot/seller-report", params={"format": "text"})
    assert "SYNTHETIC SELLER PILOT" in as_text.text
    as_md = await client.get("/pilot/seller-report", params={"format": "markdown"})
    assert as_md.text.startswith("# Synthetic seller pilot")
    assert "markdown" in as_md.headers["content-type"]
    assert (await client.get("/pilot/seller-report", params={"format": "pdf"})).status_code == 422


async def test_seller_report_route_prefers_the_database_record_and_leaves_the_investor_route(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(SELLER_REPORT_PATH_ENV, str(tmp_path / "missing.json"))
    monkeypatch.setenv("PILOT_REPORT_PATH", str(tmp_path / "missing-investor.json"))
    stored = _report()
    stored.seed = 4242
    async with make_session_factory(app.state.engine)() as s:
        await AuditRepository(s).record(
            actor=Actor.SYSTEM,
            actor_id="pilot",
            event_type=SELLER_REPORT_EVENT,
            entity_type="pilot",
            payload=stored.to_dict(),
        )
        await s.commit()
    response = await client.get("/pilot/seller-report")
    assert response.status_code == 200 and response.json()["seed"] == 4242
    # The investor route reads its own event type and is still empty.
    assert (await client.get("/pilot/report")).status_code == 404
