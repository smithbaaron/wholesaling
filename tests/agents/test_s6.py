"""S6 disposition: template guards (no seller-agreement claim beyond "under contract", no
guarantees, no exclusivity, CAN-SPAM footer, refuses unsigned), buyer selection by tier, and the
QA agent's single fact-backed exemption (REQUIREMENTS.md SEL-10, BB-09, XC-07, CMP-01, CMP-03)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.disposition_seed import (
    OPERATOR,
    seed_accepted_offer,
    seed_approved_match,
    seed_closed_deal_for,
    seed_investor,
    seed_psa,
)
from wholesaling.agents.a5 import QaDraft, QaInput, run_prechecks
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.s6 import (
    TIER_APPROVED_MATCH,
    TIER_BUY_BOX_FIT,
    UNDER_CONTRACT_FLAG,
    DealFacts,
    S6Disposition,
    S6QaAgent,
    buy_box_fits,
    deal_summary,
    evidence_summary,
)
from wholesaling.agents.templates.disposition import (
    DISPOSITION_PROHIBITED,
    ContractNotSigned,
    find_disposition_prohibited,
    render_blast,
)
from wholesaling.agents.templates.investor_outreach import ProhibitedClaimError, SenderIdentity
from wholesaling.config.settings import Settings
from wholesaling.db.models.deals import ContractStatus
from wholesaling.db.models.enums import AgentId, MatchReviewStatus, QaVerdict
from wholesaling.db.repositories.contracts import ContractRepository

SENDER = SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text='Reply "unsubscribe" to opt out.',
)


def _facts(**over: object) -> DealFacts:
    import uuid

    base: dict[str, object] = {
        "contract_id": uuid.uuid4(),
        "contract_signed": True,
        "market": "Houston",
        "property_address": "1427 Pecan Grove Dr, Houston, TX 77092",
        "property_short": "1427 Pecan Grove Dr, Houston",
        "postal_code": "77092",
        "purchase_price": Decimal("185000"),
        "assignment_fee": Decimal("15000"),
        "arv": Decimal("310000"),
        "arv_confidence": "Medium",
        "repairs": Decimal("42000"),
        "comp_tier": 1,
        "condition_class": "Medium",
        "open_flags": ["hoa_unknown"],
        "property_facts": {"beds": 3, "baths": 2, "sqft": 1640},
    }
    base.update(over)
    return DealFacts(**base)  # type: ignore[arg-type]


def _render(**over: object) -> str:
    facts = _facts(**over)
    return render_blast(
        contract_signed=facts.contract_signed,
        first_name="Jane",
        market=facts.market,
        property_address=facts.property_address,
        property_short=facts.property_short,
        deal_summary=deal_summary(facts),
        evidence_summary=evidence_summary(facts),
        sender=SENDER,
    ).body


# -- template -------------------------------------------------------------------------------------


def test_blast_says_under_contract_and_nothing_more_about_the_seller() -> None:
    body = _render()
    assert "under contract" in body
    assert "equitable interest" in body and "we are not the owner" in body  # CMP-03
    assert "Nothing is held for anyone" in body
    assert "seller has agreed only to the terms of that agreement" in body
    assert find_disposition_prohibited(body) is None
    for bad in ("seller agreed to", "seller accepted", "guaranteed", "exclusive", "act now"):
        assert bad not in body.lower()


def test_blast_carries_can_spam_footer_and_evidence() -> None:
    body = _render()
    assert body.rstrip().endswith('Reply "unsubscribe" to opt out.')
    assert "123 Main St, Houston, TX 77002" in body and "Aaron, NorthStack" in body
    assert "Assignment price: $200,000" in body  # 185k + 15k
    assert "Estimated ARV $310,000 (confidence: Medium, comp tier 1)" in body
    assert "Open items to verify: hoa_unknown" in body
    assert "3 bd / 2 ba · 1,640 sqft · condition: Medium" in body


def test_blast_refuses_unsigned_contract() -> None:
    with pytest.raises(ContractNotSigned):
        _render(contract_signed=False)


@pytest.mark.parametrize(
    "phrase",
    [
        "The seller has agreed to a 10-day close",
        "guaranteed profit",
        "exclusive deal",
        "we own this one",
    ],
)
def test_blast_refuses_seller_agreement_guarantee_or_exclusivity_in_values(phrase: str) -> None:
    facts = _facts()
    with pytest.raises(ProhibitedClaimError):
        render_blast(
            contract_signed=True,
            first_name="Jane",
            market=facts.market,
            property_address=facts.property_address,
            property_short=facts.property_short,
            deal_summary=phrase,
            evidence_summary=evidence_summary(facts),
            sender=SENDER,
        )


def test_prohibited_list_covers_the_spec_terms() -> None:
    joined = " ".join(DISPOSITION_PROHIBITED)
    assert "guaranteed" in joined and "exclusive" in joined and "seller has agreed to" in joined
    assert find_disposition_prohibited("Seller HAS   agreed to close") == "seller has agreed to"
    assert (
        find_disposition_prohibited("the seller has agreed only to the terms of that agreement")
        is None
    )


def test_missing_first_name_falls_back_to_there() -> None:
    facts = _facts()
    copy = render_blast(
        contract_signed=True,
        first_name=None,
        market=facts.market,
        property_address=facts.property_address,
        property_short=facts.property_short,
        deal_summary=deal_summary(facts),
        evidence_summary=evidence_summary(facts),
        sender=SENDER,
    )
    assert copy.body.startswith("Hi there,")
    assert copy.subject == "Under contract in Houston: 1427 Pecan Grove Dr, Houston (assignable)"
    assert copy.approval_status == "pending_approval"


def test_evidence_summary_never_prints_a_bare_number_without_a_label() -> None:
    text = evidence_summary(_facts(arv=None, repairs=None, open_flags=[]))
    assert "Estimated ARV n/a" in text and "Estimated repairs n/a" in text
    assert "none recorded; buyer performs own due diligence" in text


# -- buy box fit ----------------------------------------------------------------------------------


def _box(**over: object) -> object:
    from wholesaling.db.models.buy_boxes import BuyBox

    base: dict[str, object] = {"geography": {}, "price_min": None, "price_max": None}
    base.update(over)
    return BuyBox(investor_id=None, version=1, **base)  # type: ignore[arg-type]


def test_buy_box_fits_price_band_and_zip() -> None:
    assert buy_box_fits(
        _box(price_min=Decimal("100000"), price_max=Decimal("250000")),
        price=Decimal("200000"),
        postal_code="77092",
    )[0]  # type: ignore[arg-type]
    assert not buy_box_fits(
        _box(price_max=Decimal("150000")), price=Decimal("200000"), postal_code="77092"
    )[0]  # type: ignore[arg-type]
    assert not buy_box_fits(
        _box(price_min=Decimal("250000")), price=Decimal("200000"), postal_code="77092"
    )[0]  # type: ignore[arg-type]
    assert buy_box_fits(
        _box(geography={"zips": ["77092", "77008"]}),
        price=Decimal("200000"),
        postal_code="77092-1234",
    )[0]  # type: ignore[arg-type]
    fits, why = buy_box_fits(
        _box(geography={"zips": ["77008"]}), price=Decimal("200000"), postal_code="77092"
    )  # type: ignore[arg-type]
    assert not fits and "ZIP" in why
    assert buy_box_fits(_box(), price=None, postal_code=None)[0]  # type: ignore[arg-type]


# -- selection ------------------------------------------------------------------------------------


async def test_select_buyers_tiers_and_ordering(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    psa = await seed_psa(session, deal, signed=True)
    m = deal.market
    box_fit = {
        "price_min": Decimal("100000"),
        "price_max": Decimal("250000"),
        "geography": {"zips": ["77092"]},
    }
    # Tier 1: approved matches. "Repeat" has a prior closed deal with us -> first within tier 1.
    repeat = await seed_investor(
        session, name="Repeat Buyers LLC", email="repeat@x.com", market=m, score=60, buy_box=box_fit
    )
    top = await seed_investor(
        session, name="Top Score LLC", email="top@x.com", market=m, score=95, buy_box=box_fit
    )
    await seed_approved_match(session, investor=repeat, deal=deal)
    await seed_approved_match(session, investor=top, deal=deal)
    await seed_closed_deal_for(session, repeat, m)
    # A pending (not approved) match does not qualify for tier 1 - BB-09.
    pending = await seed_investor(
        session,
        name="Pending Match LLC",
        email="pending@x.com",
        market=m,
        score=99,
        buy_box={"price_max": Decimal("150000")},
    )
    await seed_approved_match(
        session, investor=pending, deal=deal, review_status=MatchReviewStatus.PENDING
    )
    # Tier 2: buy box fits (price + zip), no match.
    fit = await seed_investor(
        session, name="Fits Box LLC", email="fit@x.com", market=m, score=80, buy_box=box_fit
    )
    wide = await seed_investor(
        session,
        name="Market Wide LLC",
        email="wide@x.com",
        market=m,
        score=50,
        buy_box={"price_max": Decimal("300000")},
    )
    # Excluded: wrong zip, price out of band, no email, other market, no buy box.
    await seed_investor(
        session,
        name="Wrong Zip LLC",
        email="zip@x.com",
        market=m,
        buy_box={"geography": {"zips": ["77008"]}},
    )
    await seed_investor(
        session,
        name="Too Cheap LLC",
        email="cheap@x.com",
        market=m,
        buy_box={"price_max": Decimal("150000")},
    )
    await seed_investor(session, name="No Email LLC", email=None, market=m, buy_box=box_fit)
    other_market = await seed_investor(
        session, name="Dallas LLC", email="dallas@x.com", market=None, buy_box=box_fit
    )
    await seed_investor(session, name="No Box LLC", email="nobox@x.com", market=m)
    await session.commit()

    s6 = S6Disposition(SENDER)
    buyers = await s6.select_buyers(session, psa)
    names = [b.name for b in buyers]
    assert names == ["Repeat Buyers LLC", "Top Score LLC", "Fits Box LLC", "Market Wide LLC"]
    assert [b.tier for b in buyers] == [
        TIER_APPROVED_MATCH,
        TIER_APPROVED_MATCH,
        TIER_BUY_BOX_FIT,
        TIER_BUY_BOX_FIT,
    ]
    assert buyers[0].closed_deals == 1 and buyers[1].closed_deals == 0
    assert buyers[0].match_id is not None and buyers[2].buy_box_id is not None
    assert "approved D2 match" in buyers[0].reason and "price band" in buyers[2].reason
    assert other_market.id not in {b.investor_id for b in buyers}
    assert fit.id in {b.investor_id for b in buyers} and wide.id in {b.investor_id for b in buyers}

    only_matches = await s6.select_buyers(session, psa, include_fallback=False)
    assert [b.name for b in only_matches] == ["Repeat Buyers LLC", "Top Score LLC"]
    assert [b.name for b in await s6.select_buyers(session, psa, limit=1)] == ["Repeat Buyers LLC"]


async def test_select_buyers_falls_back_to_buy_boxes_when_no_match(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    psa = await seed_psa(session, deal, signed=True)
    await seed_investor(
        session,
        name="Only Box LLC",
        email="box@x.com",
        market=deal.market,
        buy_box={"price_max": Decimal("250000")},
    )
    buyers = await S6Disposition(SENDER).select_buyers(session, psa, include_fallback=False)
    assert [b.name for b in buyers] == ["Only Box LLC"] and buyers[0].tier == TIER_BUY_BOX_FIT


async def test_load_facts_reads_offer_lead_intake_and_market(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    psa = await seed_psa(session, deal, signed=True)
    facts = await S6Disposition(SENDER).load_facts(session, psa)
    assert facts.contract_signed and facts.market == "Houston"
    assert facts.property_address == "1427 Pecan Grove Dr, Houston, TX 77092"
    assert facts.asking_price == Decimal("200000") and facts.arv_confidence == "Medium"
    assert facts.condition_class == "Medium" and facts.property_facts["beds"] == 3
    assert facts.open_flags == ["hoa_unknown"]
    unsigned = await ContractRepository(session).require(psa.id)
    unsigned.status = ContractStatus.REQUESTED
    assert not (await S6Disposition(SENDER).load_facts(session, unsigned)).contract_signed


async def test_draft_from_facts_passes_guards(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    psa = await seed_psa(session, deal, signed=True)
    inv = await seed_investor(
        session,
        name="Buyer LLC",
        email="b@x.com",
        market=deal.market,
        first_name="Jane",
        buy_box={},
    )
    s6 = S6Disposition(SENDER)
    facts = await s6.load_facts(session, psa)
    buyers = await s6.select_buyers(session, psa, facts=facts)
    copy = s6.draft(facts, buyers[0])
    assert copy.body.startswith("Hi Jane,") and "under contract in Houston" in copy.body
    assert find_disposition_prohibited(copy.body) is None
    assert inv.id == buyers[0].investor_id


# -- QA exemption ---------------------------------------------------------------------------------


def _qa_input(body: str) -> QaInput:
    return QaInput(
        draft=QaDraft(to=["b@x.com"], subject="Under contract in Houston", body=body),
        thread=[],
        thread_participant="b@x.com",
        scheduled_sequence=True,
    )


async def test_s6_qa_lifts_only_the_under_contract_precheck_when_signed(
    session: AsyncSession,
) -> None:
    body = _render()
    assert UNDER_CONTRACT_FLAG in run_prechecks(_qa_input(body))  # plain A5 would block it
    settings = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    provider = MockModelProvider()
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "approve",
            "confidence": 0.93,
            "flags": [],
            "rewrite_body": None,
            "rationale": "ok",
        },
    )
    runner = AgentRunner(provider, ModelRouter.from_settings(settings))
    signed_agent = S6QaAgent(
        runner, session, correlation_id="disposition:test", contract_signed=True
    )
    result = await signed_agent.review(_qa_input(body))
    assert result.verdict is QaVerdict.APPROVE and result.run_id is not None

    unsigned_agent = S6QaAgent(
        runner, session, correlation_id="disposition:test", contract_signed=False
    )
    blocked = await unsigned_agent.review(_qa_input(body))
    assert blocked.verdict is QaVerdict.BLOCK and UNDER_CONTRACT_FLAG in blocked.flags
    assert blocked.precheck_only

    # Every other pre-check still applies with the exemption on.
    guaranteed = await signed_agent.review(_qa_input(body + "\nGuaranteed returns."))
    assert (
        guaranteed.verdict is QaVerdict.BLOCK and "prohibited_claim:guaranteed" in guaranteed.flags
    )
    assert OPERATOR  # fixture import kept for parity with the other suites
