"""D1 / D2 activities (REQUIREMENTS.md BB-04..BB-09, BB-12, WF-07, WF-20).

    run_d1   buy box version -> normalised search profile (create / coalesce / supersede) and,
             for a new profile version, exactly one ``BuyBoxSearch`` workflow start (the previous
             version's workflow gets the ``superseded`` signal)
    run_d2   one search-profile version -> candidates from the property provider's active
             listings plus off-market candidates from accepted A7 offers, deduped, estimated,
             ranked and written as ``property_candidates`` + ``matches`` (review_status pending)

Dependencies: the Graph context (session factory, settings, clock), the conversation context
(Temporal gateway) and the offer context (property provider, tunables).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from temporalio import activity

from wholesaling.activities.conversation import get_conversation_context
from wholesaling.activities.graph import get_graph_context
from wholesaling.activities.offers import get_offer_context
from wholesaling.agents.a7.comps import tiers_from_config
from wholesaling.agents.d1 import D1SearchProfileEngine
from wholesaling.agents.d2 import (
    SOURCE_A7_CONTRACT,
    CandidateEstimate,
    D2PropertyMatcher,
    estimate_candidate,
)
from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.enums import Actor, SearchProfileStatus
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.deals import CandidateRepository, SearchProfileRepository
from wholesaling.db.repositories.offers import OfferRepository
from wholesaling.integrations.data.property import ActiveListing, ListingSearch, PropertyAddress
from wholesaling.logging import bind_correlation_id, clear_context, get_logger

log = get_logger(__name__)

ACTOR_D1 = "D1"
ACTOR_D2 = "D2"
BUY_BOX_SEARCH_WORKFLOW_NAME = "BuyBoxSearch"
BUY_BOX_SEARCH_ID_PREFIX = "buy-box-search:"
SUPERSEDED_SIGNAL = "superseded"


def buy_box_search_workflow_id(buy_box_id: str, profile_version: int) -> str:
    return f"{BUY_BOX_SEARCH_ID_PREFIX}{buy_box_id}:v{profile_version}"


@dataclass(slots=True)
class BuyBoxSearchStartInput:
    """What ``BuyBoxSearch`` is started with (``workflows/buy_box_search.py`` declares the same
    fields; Temporal's converter maps by field name)."""

    search_profile_id: str
    buy_box_id: str
    profile_version: int


@dataclass(slots=True)
class SupersededPayload:
    new_version: int
    new_search_profile_id: str


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


# --------------------------------------------------------------------------------------------
# D1
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunD1Input:
    buy_box_id: str
    investor_id: str
    version: int
    event_type: str = "buy_box.updated"


@dataclass(slots=True)
class RunD1Result:
    buy_box_id: str
    search_profile_id: str
    profile_version: int
    created: bool
    coalesced: bool
    search_workflow_id: str | None
    superseded_workflow_id: str | None
    unknowns: list[str] = field(default_factory=list)
    criteria_hash: str | None = None


@activity.defn(name="deals_run_d1")
async def run_d1(input: RunD1Input) -> RunD1Result:
    """BB-04/05/06: one active profile per buy box; one search per new profile version."""
    _bind()
    ctx = get_graph_context()
    conv = get_conversation_context()
    try:
        buy_box_id = uuid.UUID(input.buy_box_id)
        async with ctx.session_factory() as session:
            box = await session.get(BuyBox, buy_box_id)
            if box is None:
                msg = f"buy box {input.buy_box_id} not found"
                raise LookupError(msg)
            normalized = D1SearchProfileEngine().normalize(box)
            repo = SearchProfileRepository(session)
            profile, created, superseded = await repo.apply(
                buy_box_id, normalized.criteria, normalized.unknowns, digest=normalized.hash
            )
            audit = AuditRepository(session)
            search_wf: str | None = None
            superseded_wf: str | None = None
            if created:
                search_wf = buy_box_search_workflow_id(input.buy_box_id, profile.version)
                if superseded is not None:
                    superseded_wf = buy_box_search_workflow_id(input.buy_box_id, superseded.version)
                    await conv.temporal.signal(
                        superseded_wf,
                        SUPERSEDED_SIGNAL,
                        SupersededPayload(
                            new_version=profile.version, new_search_profile_id=str(profile.id)
                        ),
                    )
                await conv.temporal.start_workflow(
                    BUY_BOX_SEARCH_WORKFLOW_NAME,
                    [
                        BuyBoxSearchStartInput(
                            search_profile_id=str(profile.id),
                            buy_box_id=input.buy_box_id,
                            profile_version=profile.version,
                        )
                    ],
                    workflow_id=search_wf,
                )
                await audit.record(
                    actor=Actor.AGENT,
                    actor_id=ACTOR_D1,
                    event_type="search_profile.created",
                    entity_type="search_profile",
                    entity_id=profile.id,
                    payload={
                        "buy_box_id": input.buy_box_id,
                        "buy_box_version": input.version,
                        "profile_version": profile.version,
                        "criteria_hash": profile.criteria_hash,
                        "superseded_profile_id": str(superseded.id) if superseded else None,
                        "unknowns": list(profile.unknowns),
                        "event_type": input.event_type,
                    },
                    correlation_id=input.buy_box_id,
                    idempotency_key=f"search_profile:{profile.id}:created",
                )
                await audit.record(
                    actor=Actor.SYSTEM,
                    actor_id=ACTOR_D1,
                    event_type="search.queued",
                    entity_type="search_profile",
                    entity_id=profile.id,
                    payload={
                        "workflow_id": search_wf,
                        "superseded_workflow_id": superseded_wf,
                    },
                    correlation_id=input.buy_box_id,
                    idempotency_key=f"search_profile:{profile.id}:queued",
                )
            else:
                await audit.record(
                    actor=Actor.AGENT,
                    actor_id=ACTOR_D1,
                    event_type="search_profile.coalesced",
                    entity_type="search_profile",
                    entity_id=profile.id,
                    payload={
                        "buy_box_id": input.buy_box_id,
                        "buy_box_version": input.version,
                        "profile_version": profile.version,
                        "criteria_hash": profile.criteria_hash,
                        "event_type": input.event_type,
                    },
                    correlation_id=input.buy_box_id,
                    idempotency_key=f"search_profile:{profile.id}:coalesced:v{input.version}",
                )
            await session.commit()
            log.info(
                "d1.profile_applied",
                buy_box_id=input.buy_box_id,
                profile_id=str(profile.id),
                created=created,
                version=profile.version,
            )
            return RunD1Result(
                buy_box_id=input.buy_box_id,
                search_profile_id=str(profile.id),
                profile_version=profile.version,
                created=created,
                coalesced=not created,
                search_workflow_id=search_wf,
                superseded_workflow_id=superseded_wf,
                unknowns=list(profile.unknowns),
                criteria_hash=profile.criteria_hash,
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# D2
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunD2Input:
    search_profile_id: str


@dataclass(slots=True)
class RunD2Result:
    search_profile_id: str
    candidates_seen: int
    duplicates_dropped: int
    matches_written: int
    match_ids: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def _search_from_criteria(criteria: dict[str, Any]) -> ListingSearch:
    geo = criteria.get("geography") or {}
    markets = tuple(str(m) for m in geo.get("markets") or [])
    cities = tuple(str(c) for c in geo.get("cities") or [])
    state: str | None = None
    for m in markets:
        if "," in m:
            state = m.split(",")[-1].strip()[:2].upper()
            break
    return ListingSearch(
        postal_codes=tuple(str(z) for z in geo.get("zips") or []),
        cities=cities,
        state=state,
        markets=markets,
    )


@activity.defn(name="deals_run_d2")
async def run_d2(input: RunD2Input) -> RunD2Result:
    """BB-07..09, BB-12: candidates -> dedupe -> estimates -> ranked pending matches."""
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        profile_id = uuid.UUID(input.search_profile_id)
        async with ctx.session_factory() as session:
            profiles = SearchProfileRepository(session)
            profile = await profiles.get(profile_id)
            if profile is None:
                msg = f"search profile {input.search_profile_id} not found"
                raise LookupError(msg)
            if profile.status is not SearchProfileStatus.ACTIVE:
                return RunD2Result(
                    input.search_profile_id,
                    0,
                    0,
                    0,
                    skipped_reason=f"profile is {profile.status.value}",
                )
            criteria = dict(profile.criteria)
            provider = octx.property_provider
            now = ctx.clock()
            today = now.date()
            listings = await provider.active_listings(_search_from_criteria(criteria))
            off_market: list[
                tuple[ActiveListing, uuid.UUID, uuid.UUID, Decimal | None, Decimal | None]
            ] = []
            for offer, lead in await OfferRepository(session).list_accepted_with_leads():
                facts = (offer.calc_record or {}).get("facts") or {}
                off_market.append(
                    (
                        ActiveListing(
                            listing_id=str(offer.id),
                            source=SOURCE_A7_CONTRACT,
                            address=PropertyAddress(
                                lead.property_address_line1,
                                lead.property_city,
                                lead.property_state,
                                lead.property_postal_code,
                            ),
                            parcel_id=offer.parcel_id,
                            list_price=Decimal(
                                str(
                                    (offer.calc_record or {}).get("accepted_price")
                                    or offer.offer_price
                                    or 0
                                )
                            ),
                            property_type=str(facts.get("property_type") or "sfr"),
                            units=int(facts.get("units") or 1),
                            beds=facts.get("beds"),
                            baths=Decimal(str(facts["baths"]))
                            if facts.get("baths") is not None
                            else None,
                            sqft=facts.get("sqft"),
                            year_built=facts.get("year_built"),
                            lot_sqft=facts.get("lot_sqft"),
                            condition_hint=str(
                                (offer.calc_record or {}).get("condition_class_final") or ""
                            ).lower()
                            or None,
                            status="active",
                            retrieved_at=today,
                            market=None,
                        ),
                        offer.id,
                        lead.id,
                        offer.arv,
                        offer.repairs,
                    )
                )
            all_candidates = [*listings, *(o[0] for o in off_market)]
            matcher = D2PropertyMatcher(as_of=today)
            deduped, dropped = matcher.dedupe(all_candidates)
            tiers = tiers_from_config(octx.tunables.a7)
            market_name = next(iter((criteria.get("geography") or {}).get("markets") or []), None)
            by_id = {o[0].listing_id: o for o in off_market}
            estimates: list[CandidateEstimate] = []
            for listing in deduped:
                extra = by_id.get(listing.listing_id)
                estimates.append(
                    await estimate_candidate(
                        listing,
                        provider=provider,
                        config=octx.tunables.a7,
                        tiers=tiers,
                        as_of=today,
                        market_name=market_name,
                        seller_lead_id=extra[2] if extra else None,
                        offer_id=extra[1] if extra else None,
                        known_arv=extra[3] if extra else None,
                        known_repairs=extra[4] if extra else None,
                    )
                )
            ranked = matcher.rank(criteria, estimates)
            candidates = CandidateRepository(session)
            audit = AuditRepository(session)
            match_ids: list[str] = []
            for rank, m in enumerate(ranked, start=1):
                li = m.estimate.listing
                row, _ = await candidates.upsert(
                    source=li.source,
                    external_id=li.listing_id,
                    retrieved_at=now,
                    address_line1=li.address.line1,
                    city=li.address.city,
                    state=li.address.state,
                    postal_code=li.address.postal_code,
                    address_key=li.address.key,
                    parcel_id=li.parcel_id,
                    seller_lead_id=m.estimate.seller_lead_id,
                    source_url=li.source_url,
                    list_price=li.list_price,
                    est_arv=m.estimate.arv.arv if m.estimate.arv else None,
                    est_repairs=m.estimate.repairs_total,
                    est_fees=m.estimate.fees,
                    est_assignment_spread=m.estimate.assignment_spread,
                    liquidity_score=m.estimate.liquidity_score,
                    flags=list(m.estimate.flags),
                    facts=m.estimate.facts(),
                    raw=li.to_json(),
                )
                match, created = await candidates.upsert_match(
                    search_profile_id=profile.id,
                    candidate_id=row.id,
                    score=m.score,
                    confidence=m.confidence,
                    evidence={**m.evidence, "rank": rank, "profile_version": profile.version},
                )
                match_ids.append(str(match.id))
                if created:
                    await audit.record(
                        actor=Actor.AGENT,
                        actor_id=ACTOR_D2,
                        event_type="match.created",
                        entity_type="match",
                        entity_id=match.id,
                        payload={
                            "search_profile_id": input.search_profile_id,
                            "candidate_id": str(row.id),
                            "score": str(m.score),
                            "confidence": str(m.confidence),
                            "flags": list(m.estimate.flags),
                            "review_status": "pending",
                        },
                        correlation_id=input.search_profile_id,
                        idempotency_key=f"match:{match.id}:created",
                    )
            await audit.record(
                actor=Actor.AGENT,
                actor_id=ACTOR_D2,
                event_type="search.completed",
                entity_type="search_profile",
                entity_id=profile.id,
                payload={
                    "candidates_seen": len(all_candidates),
                    "duplicates_dropped": len(dropped),
                    "dropped": dropped,
                    "matches": len(ranked),
                    "profile_version": profile.version,
                },
                correlation_id=input.search_profile_id,
                idempotency_key=f"search_profile:{profile.id}:completed:{now.isoformat()}",
            )
            await session.commit()
            log.info(
                "d2.search_completed",
                profile_id=input.search_profile_id,
                candidates=len(all_candidates),
                matches=len(ranked),
            )
            return RunD2Result(
                search_profile_id=input.search_profile_id,
                candidates_seen=len(all_candidates),
                duplicates_dropped=len(dropped),
                matches_written=len(ranked),
                match_ids=match_ids,
            )
    finally:
        clear_context()


__all__ = [
    "ACTOR_D1",
    "ACTOR_D2",
    "BUY_BOX_SEARCH_ID_PREFIX",
    "BUY_BOX_SEARCH_WORKFLOW_NAME",
    "SUPERSEDED_SIGNAL",
    "BuyBoxSearchStartInput",
    "RunD1Input",
    "RunD1Result",
    "RunD2Input",
    "RunD2Result",
    "SupersededPayload",
    "buy_box_search_workflow_id",
    "run_d1",
    "run_d2",
]
