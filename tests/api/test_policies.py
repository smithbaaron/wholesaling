"""Policy page: the 12 actions, level/cap/threshold changes as audited human actions, hard-floor
rejection, read-only POL-04 promotion readiness (POL-01..06)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import OPERATOR, seed_queued_message
from wholesaling.api.routes.policies import EVENT_POLICY_CHANGED
from wholesaling.db.models.enums import Actor, QaVerdict
from wholesaling.db.models.ops import AuditEvent, Policy
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.policy.actions import HARD_FLOOR_ACTIONS, AutonomyLevel, PolicyAction
from wholesaling.reporting.kpis import (
    EVENT_APPROVED,
    EVENT_APPROVED_WITH_EDITS,
    PROMOTION_MIN_APPROVED,
    promotion_readiness,
)

SF = async_sessionmaker[AsyncSession]


async def _policy_events(sf: SF) -> list[AuditEvent]:
    async with sf() as s:
        stmt = select(AuditEvent).where(AuditEvent.event_type == EVENT_POLICY_CHANGED)
        return list((await s.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------------------------


async def test_json_lists_all_twelve_actions_at_l0_without_seed(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/api/policies")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 12
    assert [p["action"] for p in body["policies"]] == [a.value for a in PolicyAction]
    assert {p["level"] for p in body["policies"]} == {"L0"}
    by_action = {p["action"]: p for p in body["policies"]}
    assert by_action["investor_initial_outreach"]["behaviour"] == "draft"
    assert by_action["investor_initial_outreach"]["levels"]["L1"] == {
        "behaviour": "auto_if",
        "condition": "QA-approved; daily cap",
    }
    assert by_action["seller_outreach_sms"]["behaviour"] == "off"
    assert by_action["opt_out_suppression"]["behaviour"] == "auto"
    assert by_action["contract_execution"]["hard_floor"] is True
    assert by_action["earnest_money"]["hard_floor"] is True
    assert by_action["investor_reply"]["threshold"] is None
    assert by_action["investor_reply"]["threshold_effective"] == 0.85
    assert by_action["investor_reply"]["readiness"]["ready"] is False


async def test_json_reflects_seeded_rows(auth_client: AsyncClient, session_factory: SF) -> None:
    async with session_factory() as s:
        repo = PolicyRepository(s)
        await repo.ensure_seeded()
        await repo.set_level(
            PolicyAction.INVESTOR_REPLY,
            AutonomyLevel.L1,
            updated_by="setup",
            daily_cap=25,
            threshold=Decimal("0.9"),
            note="pilot",
        )
        await s.commit()
    body = (await auth_client.get("/api/policies")).json()
    p = {x["action"]: x for x in body["policies"]}["investor_reply"]
    assert p["level"] == "L1"
    assert p["behaviour"] == "auto_if"
    assert p["daily_cap"] == 25
    assert p["threshold"] == 0.9
    assert p["updated_by"] == "setup"
    assert p["note"] == "pilot"


async def test_html_renders_table_with_hard_floors_disabled(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/policies")
    assert r.status_code == 200
    html = r.text
    for action in PolicyAction:
        assert f'id="policy-{action.value}"' in html
    assert html.count("hard floor</span>") == len(HARD_FLOOR_ACTIONS)
    assert 'disabled title="hard floor: stays L0"' in html
    assert "Promotion readiness" in html
    assert "0 / 50 approved drafts" in html
    assert 'hx-post="/policies/investor_initial_outreach"' in html
    assert OPERATOR in html


# --------------------------------------------------------------------------------------------
# Changing a level: a human-visible audited event (POL-01)
# --------------------------------------------------------------------------------------------


async def test_level_change_writes_audit_event_with_operator(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    r = await auth_client.post(
        "/policies/investor_initial_outreach",
        data={"level": "L1", "daily_cap": "20", "threshold": "0.9", "note": "pilot week 1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["policy"]["level"] == "L1"
    assert body["policy"]["daily_cap"] == 20
    assert body["policy"]["threshold"] == 0.9
    assert body["policy"]["updated_by"] == OPERATOR
    assert body["before"] == {"level": "L0", "daily_cap": None, "threshold": None, "note": None}
    assert body["after"] == {
        "level": "L1",
        "daily_cap": 20,
        "threshold": "0.9",
        "note": "pilot week 1",
    }

    async with session_factory() as s:
        row = (
            await s.execute(
                select(Policy).where(Policy.action == PolicyAction.INVESTOR_INITIAL_OUTREACH)
            )
        ).scalar_one()
        assert row.level is AutonomyLevel.L1
        assert row.updated_by == OPERATOR

    events = await _policy_events(session_factory)
    assert len(events) == 1
    ev = events[0]
    assert ev.actor is Actor.HUMAN
    assert ev.actor_id == OPERATOR
    assert ev.entity_type == "policy"
    assert ev.payload["action"] == "investor_initial_outreach"
    assert ev.payload["before"]["level"] == "L0" and ev.payload["after"]["level"] == "L1"


async def test_level_change_htmx_returns_row_fragment(auth_client: AsyncClient) -> None:
    r = await auth_client.post(
        "/policies/investor_followup", data={"level": "l2"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert 'id="policy-investor_followup"' in r.text
    assert "lvl-L2" in r.text and "<html" not in r.text


async def test_cap_only_change_keeps_level(auth_client: AsyncClient, session_factory: SF) -> None:
    r = await auth_client.post("/policies/seller_outreach_email", data={"daily_cap": "10"})
    assert r.status_code == 200
    assert r.json()["policy"]["level"] == "L0"
    assert r.json()["policy"]["daily_cap"] == 10
    assert len(await _policy_events(session_factory)) == 1


async def test_demotion_is_allowed_and_audited(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    assert (
        await auth_client.post("/policies/investor_reply", data={"level": "L2"})
    ).status_code == 200
    r = await auth_client.post("/policies/investor_reply", data={"level": "L0"})
    assert r.status_code == 200
    assert r.json()["before"]["level"] == "L2" and r.json()["after"]["level"] == "L0"
    assert len(await _policy_events(session_factory)) == 2


async def test_hard_floor_cannot_be_raised(auth_client: AsyncClient, session_factory: SF) -> None:
    for action in HARD_FLOOR_ACTIONS:
        for level in ("L1", "L2"):
            r = await auth_client.post(f"/policies/{action.value}", data={"level": level})
            assert r.status_code == 400, r.text
            assert "hard floor" in r.json()["detail"]
            assert "POL-06" in r.json()["detail"]
    # Nothing was written, nothing audited.
    assert await _policy_events(session_factory) == []
    async with session_factory() as s:
        rows = await PolicyRepository(s).all()
        assert all(r.level is AutonomyLevel.L0 for r in rows.values())
    body = (await auth_client.get("/api/policies")).json()
    assert {p["level"] for p in body["policies"]} == {"L0"}


async def test_hard_floor_htmx_rejection_is_a_fragment(auth_client: AsyncClient) -> None:
    r = await auth_client.post(
        "/policies/contract_execution", data={"level": "L1"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 400
    assert "Rejected" in r.text and "hard floor" in r.text


async def test_hard_floor_can_still_take_a_note_at_l0(auth_client: AsyncClient) -> None:
    r = await auth_client.post("/policies/earnest_money", data={"note": "attorney only"})
    assert r.status_code == 200
    assert r.json()["policy"]["level"] == "L0"
    assert r.json()["policy"]["note"] == "attorney only"


async def test_validation_errors(auth_client: AsyncClient) -> None:
    assert (await auth_client.post("/policies/nope", data={"level": "L1"})).status_code == 404
    assert (
        await auth_client.post("/policies/investor_reply", data={"level": "L9"})
    ).status_code == 422
    assert (
        await auth_client.post("/policies/investor_reply", data={"daily_cap": "-1"})
    ).status_code == 422
    assert (
        await auth_client.post("/policies/investor_reply", data={"daily_cap": "ten"})
    ).status_code == 422
    assert (
        await auth_client.post("/policies/investor_reply", data={"threshold": "1.5"})
    ).status_code == 422
    assert (
        await auth_client.post("/policies/investor_reply", data={"threshold": "abc"})
    ).status_code == 422
    assert (await auth_client.post("/policies/investor_reply", data={})).status_code == 422


# --------------------------------------------------------------------------------------------
# POL-04 promotion readiness math
# --------------------------------------------------------------------------------------------


async def _approve_n(
    s: AsyncSession,
    action: str,
    n: int,
    *,
    edited: int = 0,
    at: datetime | None = None,
) -> None:
    audit = AuditRepository(s)
    for i in range(n):
        seeded = await seed_queued_message(s, action=action)
        ev, _ = await audit.record(
            actor=Actor.HUMAN,
            actor_id="aaron",
            event_type=EVENT_APPROVED_WITH_EDITS if i < edited else EVENT_APPROVED,
            entity_type="message",
            entity_id=seeded.message.id,
            payload={"action": action},
        )
        if at is not None:
            ev.created_at = at
    await s.flush()


async def test_readiness_empty(session_factory: SF) -> None:
    async with session_factory() as s:
        r = await promotion_readiness(s, PolicyAction.INVESTOR_INITIAL_OUTREACH)
    assert r.approved_drafts == 0
    assert r.edit_rate is None
    assert r.ready is False
    assert r.criteria == {
        "approved_drafts": False,
        "edit_rate": False,
        "no_qa_blocked_sent": True,
        "no_compliance_flags": True,
        "not_hard_floor": True,
    }
    assert r.demotion_signal is False


async def test_readiness_meets_all_criteria(session_factory: SF) -> None:
    async with session_factory() as s:
        await _approve_n(s, "investor_initial_outreach", PROMOTION_MIN_APPROVED, edited=2)
        await s.commit()
        r = await promotion_readiness(s, PolicyAction.INVESTOR_INITIAL_OUTREACH)
    assert r.approved_drafts == 50
    assert r.edited_drafts == 2
    assert r.edit_rate == 0.04
    assert r.ready is True
    assert r.demotion_signal is False
    assert r.as_dict()["ready"] is True


async def test_readiness_fails_on_edit_rate(session_factory: SF) -> None:
    async with session_factory() as s:
        await _approve_n(s, "investor_initial_outreach", 60, edited=4)  # 6.7 %
        await s.commit()
        r = await promotion_readiness(s, PolicyAction.INVESTOR_INITIAL_OUTREACH)
    assert r.criteria["approved_drafts"] is True
    assert r.criteria["edit_rate"] is False
    assert r.ready is False


async def test_readiness_counts_only_this_action_and_window(session_factory: SF) -> None:
    async with session_factory() as s:
        await _approve_n(s, "investor_initial_outreach", 10)
        await _approve_n(s, "investor_followup", 7)
        await _approve_n(
            s, "investor_initial_outreach", 5, at=datetime.now(tz=UTC) - timedelta(days=45)
        )
        await s.commit()
        r = await promotion_readiness(s, PolicyAction.INVESTOR_INITIAL_OUTREACH)
        f = await promotion_readiness(s, PolicyAction.INVESTOR_FOLLOWUP)
    assert r.approved_drafts == 10
    assert f.approved_drafts == 7


async def test_readiness_qa_blocked_sent_and_compliance_flags(session_factory: SF) -> None:
    async with session_factory() as s:
        await _approve_n(s, "investor_reply", PROMOTION_MIN_APPROVED)
        # A QA-blocked draft that was nonetheless sent.
        bad = await seed_queued_message(
            s, action="investor_reply", qa_verdict=QaVerdict.BLOCK, draft=False
        )
        bad.message.sent_at = datetime.now(tz=UTC)
        # A compliance flag on another draft.
        await seed_queued_message(
            s, action="investor_reply", qa_flags=["compliance:missing_opt_out", "tone"]
        )
        await s.commit()
        r = await promotion_readiness(s, PolicyAction.INVESTOR_REPLY)
    assert r.qa_blocked_sent == 1
    assert r.compliance_flags == 1
    assert r.criteria["no_qa_blocked_sent"] is False
    assert r.criteria["no_compliance_flags"] is False
    assert r.ready is False
    assert r.demotion_signal is True  # any compliance incident


async def test_readiness_hard_floor_never_ready(session_factory: SF) -> None:
    async with session_factory() as s:
        await _approve_n(s, "contract_execution", PROMOTION_MIN_APPROVED)
        await s.commit()
        r = await promotion_readiness(s, PolicyAction.CONTRACT_EXECUTION)
    assert r.hard_floor is True
    assert r.criteria["not_hard_floor"] is False
    assert r.ready is False


async def test_readiness_7day_demotion_signal(session_factory: SF) -> None:
    async with session_factory() as s:
        await _approve_n(s, "investor_followup", 10, edited=2)  # 20 % in the last 7 days
        await s.commit()
        r = await promotion_readiness(s, PolicyAction.INVESTOR_FOLLOWUP)
    assert r.edit_rate_7d == 0.2
    assert r.demotion_signal is True


async def test_readiness_shows_up_in_policy_json(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        await _approve_n(s, "investor_initial_outreach", 3, edited=1)
        await s.commit()
    body = (await auth_client.get("/api/policies")).json()
    p = {x["action"]: x for x in body["policies"]}["investor_initial_outreach"]
    assert p["readiness"]["approved_drafts"] == 3
    assert p["readiness"]["edited_drafts"] == 1
    assert p["readiness"]["ready"] is False
    assert p["readiness"]["window_days"] == 30


async def test_plain_browser_form_post_redirects_to_table(auth_client: AsyncClient) -> None:
    r = await auth_client.post(
        "/policies/investor_reply", data={"level": "L1"}, headers={"Accept": "text/html"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/policies"
