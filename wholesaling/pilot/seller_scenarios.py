"""The synthetic seller pilot's personas (REQUIREMENTS.md BO-14, BO-20, SEL-01..SEL-12, A7-12..17,
POL-03d..k).

Every persona is one seller: either a lead S1 builds from the mock distress fixture (Houston,
Active) and S2 traces, a lead the pilot seeds by hand because the fixture has only four
reachable owners (reported as GAP-S01), or a homeowner who fills in the public ``/sell`` web
form on one of the A7 fixture subjects. The persona scripts what the seller does afterwards
(nothing, a reply, an opt-out, a second reply, an acceptance, a counter) and what the *mock*
model must answer for S4 (classify / extract / draft) and A5 (QA) on each message, so the
deterministic machinery around the model runs on realistic structured output.

``SellerScenario.expected(level)`` is the end state the report grades: the seller lead, the
sequence, the intake, the offer (status, confidence, failed gates, verification passes), the
negotiation, the contract and the disposition. ``known_defects`` names the checks a documented
gap (docs/pilot.md, "Seller pilot") makes fail today, so a scenario failing only on those is
reported as ``known_defect`` rather than ``fail``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from wholesaling.policy.actions import AutonomyLevel, PolicyAction

SELLER_MARKET = "Houston"
SELLER_MARKET_STATE = "TX"
WATCH_MARKET = "Columbus"  # stays Watch: S1 must refuse it (SEL-01)

SELLER_LEVEL_ACTIONS: tuple[PolicyAction, ...] = (
    PolicyAction.SELLER_OUTREACH_EMAIL,
    PolicyAction.SELLER_INTAKE_REPLY,
    PolicyAction.OFFER_SEND,
    PolicyAction.COUNTER_ACCEPTANCE,
    PolicyAction.BUYER_DISPOSITION_BLAST,
)

Trigger = Literal["touch", "s4_reply", "offer_sent"]
LeadSource = Literal["s1", "seed", "web_form"]
DealPlan = Literal["close", "dead_after_inspection"]


# --------------------------------------------------------------------------------------------
# Mock-model script helpers (S4 / A5 structured outputs)
# --------------------------------------------------------------------------------------------


def s4_classify(
    label: str, confidence: float = 0.92, *, asks_offer: bool = False, rationale: str = ""
) -> dict[str, Any]:
    return {
        "classification": label,
        "confidence": confidence,
        "rationale": rationale or f"seller message reads as {label}",
        "is_auto_reply": False,
        "asks_offer": asks_offer,
    }


def s4_extract(rationale: str = "stated facts only", **fields: Any) -> dict[str, Any]:
    """One ``EXTRACT_SCHEMA`` object; anything not stated stays ``null`` (never invented)."""
    base: dict[str, Any] = {
        "property_address": None,
        "motivation": None,
        "condition_class": None,
        "disclosed_issues": [],
        "timeline": None,
        "timeline_text": None,
        "asking_price": None,
        "co_owners": None,
        "occupancy": None,
        "liens": None,
        "hoa": None,
        "seller_unsure": [],
        "photos_mentioned": False,
        "rationale": rationale,
    }
    base.update(fields)
    return base


def s4_draft(body: str, *asks: str, sensitive: bool = False) -> dict[str, Any]:
    return {"subject": "", "body": body, "asks_for": list(asks), "contains_sensitive": sensitive}


def qa_approve(confidence: float = 0.93, rationale: str = "") -> dict[str, Any]:
    return {
        "verdict": "approve",
        "confidence": confidence,
        "flags": [],
        "rewrite_body": None,
        "rationale": rationale or "Accurate to the thread, asks only for what is missing.",
    }


@dataclass(frozen=True, slots=True)
class S4Script:
    classify: dict[str, Any] | None = None
    extract: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None


# A5 verdicts for every locked template the seller side sends (sequence steps, the offer, the
# S5 reminders and re-engagements, the S6 blast). Keyed on a phrase of the rendered body.
SELLER_TEMPLATE_QA: dict[str, dict[str, Any]] = {
    "We buy houses in the": qa_approve(0.96, "SEL-04 initial: disclosure + footer present."),
    "Following up on my note about": qa_approve(0.95, "SEL-04 follow-up 1."),
    "Checking in once more about": qa_approve(0.95, "SEL-04 follow-up 2."),
    "This is my last note about": qa_approve(0.95, "SEL-04 follow-up 3."),
    "we can offer": qa_approve(0.95, "A7-09 locked offer template; number from the record."),
    "Following up on the cash offer": qa_approve(0.95, "S5 reminder, locked template."),
    "Our earlier offer has lapsed": qa_approve(0.95, "S5 re-engagement, no number."),
    "may fit your buy box": qa_approve(0.94, "S6 blast: under contract, signed row on record."),
}


# --------------------------------------------------------------------------------------------
# Scripted seller messages and negotiation moves
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NegotiationMove:
    """What the pilot signals to ``NegotiationWorkflow`` once the seller's reply is in (the
    inbound pipeline does not route post-offer replies to S5 today, GAP-S04)."""

    kind: Literal["accept", "counter", "decline"]
    #: counter amount as a fraction of the negotiation room: 0 = our offer, 1 = MAO, >1 above MAO
    room_fraction: float = 0.0
    #: what the operator does when the counter waits for a human (L0 or above MAO)
    human_override: Literal["accept", "decline"] | None = None


@dataclass(frozen=True, slots=True)
class SellerMessage:
    """One inbound email the seller sends ``delay_hours`` after ``trigger`` number ``index``
    (touch 1 = the initial; s4_reply 1 = our first intake reply; offer_sent)."""

    trigger: Trigger
    delay_hours: float
    body: str
    match: str  # phrase of the body the mock model's scripts key on
    model: S4Script = field(default_factory=S4Script)
    index: int = 1
    negotiation: NegotiationMove | None = None


@dataclass(frozen=True, slots=True)
class WebForm:
    """A ``POST /sell`` the homeowner makes ``at_hours`` after the pilot starts."""

    at_hours: float
    address_line1: str
    postal_code: str
    name: str
    email: str
    city: str = "Houston"
    state: str = "TX"
    motivation: str = ""
    condition_class: str = ""
    disclosed_issues: str = ""
    timeline: str = ""
    asking_price: str = ""
    phone: str = ""
    photos: int = 1


@dataclass(frozen=True, slots=True)
class PropertyAlias:
    """Serve one A7 fixture subject under the persona's own address (the mock property provider
    knows only the seven fixture addresses, none of which S1's distress fixture lists: GAP-S02)."""

    fixture_line1: str  # one of the seven properties.json subjects
    fixture_postal_code: str


@dataclass(frozen=True, slots=True)
class SeedLead:
    """A lead the pilot enters by hand (GAP-S01: the distress fixture has four reachable owners)."""

    owner_name: str
    address_line1: str
    postal_code: str
    email: str
    signal_score: int = 55


@dataclass(frozen=True, slots=True)
class SellerScenario:
    id: str
    title: str
    source: LeadSource
    #: ``investors.canonical_name``-style key: the owner name S1 writes (s1), the seeded owner
    #: (seed) or the form's name (web_form). ``None`` for a cohort check.
    owner: str | None
    address_line1: str | None = None
    postal_code: str | None = None
    form: WebForm | None = None
    seed: SeedLead | None = None
    alias: PropertyAlias | None = None
    messages: tuple[SellerMessage, ...] = ()
    #: inject three recent comps before the first V6 pre-send check (A7-17)
    inject_drift_comps: bool = False
    #: patch the engine's MAO step for this one computation so V5 catches the mismatch (A7-16)
    inject_v5_mismatch: bool = False
    #: what the operator does on /deals after ``contract.requested``
    deal_plan: DealPlan | None = None
    covers: tuple[str, ...] = ()
    #: level-independent expectations, then per-level overrides
    expect: Mapping[str, Any] = field(default_factory=dict)
    expect_by_level: Mapping[AutonomyLevel, Mapping[str, Any]] = field(default_factory=dict)
    known_defects: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_cohort_check(self) -> bool:
        return self.owner is None

    def expected(self, level: AutonomyLevel) -> dict[str, Any]:
        out = dict(self.expect)
        out.update(self.expect_by_level.get(level, {}))
        return out


# --------------------------------------------------------------------------------------------
# Shared message text
# --------------------------------------------------------------------------------------------

_FULL_INFO_EXTRACT = dict(
    motivation="landlord tired of managing a rental from out of town",
    condition_class="Medium",
    disclosed_issues=["roof"],
    timeline="within_30_days",
    co_owners="none",
    occupancy="vacant",
    liens="none",
    hoa="no",
)


def _full_info_message(
    *, trigger: Trigger, index: int, delay_hours: float, match: str, body: str, **extract: Any
) -> SellerMessage:
    fields = {**_FULL_INFO_EXTRACT, **extract}
    return SellerMessage(
        trigger=trigger,
        index=index,
        delay_hours=delay_hours,
        body=body,
        match=match,
        model=S4Script(
            classify=s4_classify("reply", 0.94),
            extract=s4_extract(**fields),
        ),
    )


# --------------------------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------------------------

GAP_S03 = "GAP-S03"  # web form never asks co-owners -> every form offer fails no_open_flags
GAP_S04 = "GAP-S04"  # post-offer seller replies are not routed to S5 by the inbound pipeline
GAP_S05 = "GAP-S05"  # NegotiationWorkflow has no human_decision: queued reminders unapprovable
GAP_S08 = "GAP-S08"  # A5 cadence pre-check BLOCKS a reply to a seller who answered within 24 h
GAP_S09 = "GAP-S09"  # "sole owner" is read as unidentified co-owners -> co_owner_missing flag
GAP_S11 = "GAP-S11"  # seller lead status never advances past "offered" (no under_contract/closed)
GAP_S12 = "GAP-S12"  # A7 templates carry a second TX disclosure wording (not the SEL-05 block)

_RAMIREZ = "Dolores Ramirez"
_NGUYEN = "Thomas Nguyen"
_PIKE = "Gerald Pike"
_OKAFOR = "Linda & Mark Okafor"

CLEAN = PropertyAlias("1204 Oak Hollow Dr", "77008")
TIER2 = PropertyAlias("88 Pine Ridge Ln", "77018")
SQFT_GAP = PropertyAlias("4510 Maple Ct", "77092")

SELLER_SCENARIOS: tuple[SellerScenario, ...] = (
    # -- S1 leads -----------------------------------------------------------------------------
    SellerScenario(
        id="s1_partial_intake_to_dead_deal",
        title=(
            "S1 lead replies with partial facts, S4 asks two fields, the second reply completes "
            "the intake; A7 offers, the seller accepts, a human signs, S6 blasts, the deal dies "
            "after inspection"
        ),
        source="s1",
        owner=_RAMIREZ,
        address_line1="1420 Pecan Grove Dr",
        postal_code="77009",
        alias=CLEAN,
        messages=(
            SellerMessage(
                trigger="touch",
                index=1,
                delay_hours=26,
                body=(
                    "Hi, yes I have thought about selling. I moved out to Katy a few years ago "
                    "and I am tired of dealing with the house from a distance. It needs work: "
                    "the roof leaks in the back bedroom and the kitchen is original."
                ),
                match="tired of dealing with the house",
                model=S4Script(
                    classify=s4_classify("reply", 0.95),
                    extract=s4_extract(
                        motivation="moved to Katy; tired of managing the house from a distance",
                        condition_class="Medium",
                        disclosed_issues=["roof"],
                    ),
                    draft=s4_draft(
                        "Thank you for telling me about the roof and the kitchen - that helps. "
                        "Two quick questions so I can put a number together: how soon would you "
                        "want to close, and is anyone else on the title with you?",
                        "timeline",
                        "co_owners",
                    ),
                    qa=qa_approve(0.94),
                ),
            ),
            SellerMessage(
                trigger="s4_reply",
                index=1,
                delay_hours=20,
                body=(
                    "As soon as possible, this month if we can. I am the only one on the title - "
                    "no mortgage, no HOA. Nobody lives there right now."
                ),
                match="only one on the title",
                model=S4Script(
                    classify=s4_classify("reply", 0.93),
                    extract=s4_extract(
                        timeline="asap",
                        co_owners="none",
                        liens="none",
                        hoa="no",
                        occupancy="vacant",
                    ),
                ),
            ),
            SellerMessage(
                trigger="offer_sent",
                delay_hours=26,
                body="That number works for me. Send over the agreement and let's do it.",
                match="That number works for me",
                model=S4Script(classify=s4_classify("reply", 0.9), extract=s4_extract()),
                negotiation=NegotiationMove("accept"),
            ),
        ),
        deal_plan="dead_after_inspection",
        covers=("SEL-04", "SEL-06", "SEL-07", "A7-01", "SEL-11", "SEL-10", "WF-06", "POL-03i"),
        expect={
            "lead_found": True,
            "parcel_verified": True,
            "sequence_status": "cancelled",
            "cancel_reason": "reply",
            "touches_sent": 1,
            "inbound_rows": 4,  # two replies, the acceptance, the day-45 duplicate web form
            "classifications": ["interested", "interested", "interested", "interested"],
            "s4_replies": 1,
            "s4_replies_sent": 1,
            "s4_sensitive": 0,
            "photo_link_sent": True,
            "intake_complete": True,
            "offer_status": "accepted",
            "arv_confidence": "Medium",  # High less one level: no photos (SEL-06)
            "gates_failed": [],
            "offer_sent": True,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
            "v5_results": ["pass"],
            "contract_requested": True,
            "negotiation_outcome": "accepted",
            "contract_status": "cancelled",
            "blast_recipients": 3,
            "deal_dead_reason": "inspection: foundation movement not disclosed",
            "earnest_release_reminder": True,
            "disposition_outcome": "dead",
            "lead_status": "offered",
        },
        expect_by_level={
            AutonomyLevel.L0: {"s4_replies_auto": 0, "offer_auto": False, "blast_auto": 0},
            AutonomyLevel.L1: {"s4_replies_auto": 1, "offer_auto": True, "blast_auto": 3},
        },
    ),
    SellerScenario(
        id="s1_full_reply_counter_inside_room",
        title=(
            "S1 lead (DNC-listed phone) gives everything in one reply; A7 offers; the seller "
            "counters inside the negotiation room (L1 auto-accept, L0 human)"
        ),
        source="s1",
        owner=_NGUYEN,
        address_line1="915 Linden Ct",
        postal_code="77018",
        alias=TIER2,
        messages=(
            _full_info_message(
                trigger="touch",
                index=1,
                delay_hours=28,
                match="manage it from Dallas",
                body=(
                    "I own it as a rental and manage it from Dallas, which is getting old. It is "
                    "in decent shape, the roof is about 15 years old. Vacant since August. I am "
                    "the only owner, no loan on it and no HOA. I could close within 30 days. I "
                    "was hoping for something around what the tax office says."
                ),
                motivation="tired landlord managing a rental from Dallas",
                disclosed_issues=[],
                co_owners="sole owner",  # what a model writes; gates read it as unidentified
            ),
            SellerMessage(
                trigger="offer_sent",
                delay_hours=30,
                body="I appreciate the offer. Could you do a little better? Meet me in the middle.",
                match="Meet me in the middle",
                model=S4Script(classify=s4_classify("reply", 0.9), extract=s4_extract()),
                negotiation=NegotiationMove("counter", room_fraction=0.6, human_override="accept"),
            ),
        ),
        covers=("SEL-03", "CMP-02", "SEL-09", "POL-03h", "A7-03"),
        expect={
            "lead_found": True,
            "dnc_listed": True,
            "sequence_status": "cancelled",
            "cancel_reason": "reply",
            "touches_sent": 1,
            "inbound_rows": 2,
            "s4_replies": 0,
            "intake_complete": True,
            "offer_status": "accepted",
            "arv_confidence": "Low",  # Medium (tier 2) less one level: no photos
            "gates_failed": ["arv_confidence"],
            "offer_sent": True,
            "offer_auto": False,
            "counters": 1,
            "counter_within_room": True,
            "contract_requested": True,
            "negotiation_outcome": "accepted",
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
        },
        expect_by_level={
            AutonomyLevel.L0: {"counter_decision": "queue_for_human", "accepted_by": "human"},
            AutonomyLevel.L1: {"counter_decision": "send", "accepted_by": "S5"},
        },
        known_defects={GAP_S09: ("gates_failed",)},
    ),
    SellerScenario(
        id="s1_counter_above_mao_human_declines",
        title=(
            "S1 lead gives everything; A7 offers; the seller counters above MAO -> a human "
            "decides, declines; the lead is requeued 90 days later (SEL-09)"
        ),
        source="s1",
        owner=_PIKE,
        address_line1="4410 Mesquite Trl",
        postal_code="77045",
        alias=CLEAN,
        messages=(
            _full_info_message(
                trigger="touch",
                index=1,
                delay_hours=27,
                match="duplex",
                body=(
                    "It is a duplex I used to live in; both sides are empty now and the listing "
                    "expired last spring. Average condition, roof is fine, one AC unit is out. I "
                    "am the only owner, it is paid off, no HOA. I could close in 60 to 90 days."
                ),
                motivation="expired listing; both units vacant",
                disclosed_issues=["hvac"],
                timeline="within_90_days",
            ),
            SellerMessage(
                trigger="offer_sent",
                delay_hours=40,
                body="Too low. I would need a lot more than that to let it go.",
                match="I would need a lot more",
                model=S4Script(classify=s4_classify("reply", 0.9), extract=s4_extract()),
                negotiation=NegotiationMove("counter", room_fraction=1.6, human_override="decline"),
            ),
        ),
        covers=("SEL-09", "POL-03h", "POL-06"),
        expect={
            "lead_found": True,
            "sequence_status": "cancelled",
            "cancel_reason": "reply",
            "touches_sent": 1,
            "inbound_rows": 2,
            "intake_complete": True,
            "offer_status": "declined",
            "arv_confidence": "Medium",
            "gates_failed": [],
            "offer_sent": True,
            "counters": 1,
            "counter_within_room": False,
            "counter_decision": "queue_for_human",
            "contract_requested": False,
            "negotiation_outcome": "declined",
            "requeued": True,
            "lead_status": "queued",
        },
        expect_by_level={
            AutonomyLevel.L0: {"offer_auto": False},
            AutonomyLevel.L1: {"offer_auto": True},
        },
    ),
    SellerScenario(
        id="s1_offer_expires_then_reengage",
        title=(
            "S1 lead gives everything (county/provider sqft disagree -> V1 third source); A7 "
            "offers; no answer: reminders at 2/5/10 d, expiry at 14 d, re-engagement at 60/90 d"
        ),
        source="s1",
        owner=_OKAFOR,
        address_line1="560 Cypress Bend",
        postal_code="77084",
        alias=SQFT_GAP,
        messages=(
            _full_info_message(
                trigger="touch",
                index=1,
                delay_hours=30,
                match="inherited nothing",
                body=(
                    "We inherited nothing but headaches with this one - just kidding, we bought it "
                    "in 2016 as a rental and the tenants left it rough. Medium condition, some "
                    "water damage in a bathroom. We both own it (my husband Mark and I), no "
                    "mortgage, no HOA, vacant. Flexible on timing."
                ),
                motivation="former rental left rough by tenants",
                disclosed_issues=["water damage"],
                timeline="flexible",
                co_owners="Linda and Mark Okafor, both identified on title",
            ),
        ),
        covers=("A7-12", "SEL-09", "WF-05"),
        expect={
            "lead_found": True,
            "sequence_status": "cancelled",
            "cancel_reason": "reply",
            "touches_sent": 1,
            "inbound_rows": 1,
            "intake_complete": True,
            "offer_status": "expired",
            "arv_confidence": "Medium",
            "gates_failed": [],
            "offer_sent": True,
            "v1_result": "adjusted",
            "reminders_sent": [2, 5, 10],  # the workflow's timeline (queued counts as "sent")
            "reengagements_sent": [60, 90],
            "reminders_delivered": 3,  # rows actually sent: 0 at L0 until GAP-S05 is fixed
            "reengagements_delivered": 2,
            "negotiation_outcome": "expired",
            "contract_requested": False,
        },
        expect_by_level={
            AutonomyLevel.L0: {"offer_auto": False, "reminders_queued": 3},
            AutonomyLevel.L1: {"offer_auto": True, "reminders_queued": 0},
        },
        known_defects={GAP_S05: ("reminders_delivered", "reengagements_delivered")},
    ),
    # -- seeded leads (GAP-S01) ----------------------------------------------------------------
    SellerScenario(
        id="seed_no_reply",
        title="never answers: initial + three follow-ups at 3/7/14 days, sequence completed",
        source="seed",
        owner="Quiet Owner",
        address_line1="101 Silent St",
        postal_code="77008",
        seed=SeedLead("Quiet Owner", "101 Silent St", "77008", "quiet.owner@example.test", 58),
        covers=("SEL-04", "WF-03"),
        expect={
            "lead_found": True,
            "sequence_status": "completed",
            "cancel_reason": None,
            "touches_sent": 4,
            "inbound_rows": 0,
            "intake_complete": False,
            "offer_status": None,
            "lead_status": "in_sequence",
        },
        expect_by_level={
            AutonomyLevel.L0: {"auto_touches": 0},
            AutonomyLevel.L1: {"auto_touches": 4},
        },
    ),
    SellerScenario(
        id="seed_hostile_opt_out",
        title="hostile opt-out after the initial: suppression, lead suppressed, no reply drafted",
        source="seed",
        owner="Hostile Owner",
        address_line1="202 Angry Ave",
        postal_code="77008",
        seed=SeedLead("Hostile Owner", "202 Angry Ave", "77008", "hostile.owner@example.test"),
        messages=(
            SellerMessage(
                trigger="touch",
                index=1,
                delay_hours=1,
                body=(
                    "Stop emailing me. I never asked for this. Take me off whatever list you "
                    "bought or I will report you."
                ),
                match="Take me off whatever list",
                model=S4Script(classify=s4_classify("opt_out", 0.98)),
            ),
        ),
        covers=("POL-03l", "SEL-05", "CMP-01"),
        expect={
            "lead_found": True,
            "sequence_status": "cancelled",
            "cancel_reason": "opt_out",
            "touches_sent": 1,
            "inbound_rows": 1,
            "classifications": ["opt_out"],
            "s4_replies": 0,
            "suppression_reason": "opt_out",
            "lead_status": "suppressed",
            "offer_status": None,
        },
    ),
    SellerScenario(
        id="seed_asks_for_a_number",
        title=(
            '"what\'s your offer?" before any facts: S4 asks two fields, the draft is sensitive '
            "(POL-09) and waits for a human at every level"
        ),
        source="seed",
        owner="Price Asker",
        address_line1="303 Number Ln",
        postal_code="77008",
        seed=SeedLead("Price Asker", "303 Number Ln", "77008", "price.asker@example.test"),
        messages=(
            SellerMessage(
                trigger="touch",
                index=1,
                delay_hours=2,
                body="What's your offer? Give me a number first and then we can talk details.",
                match="Give me a number first",
                model=S4Script(
                    classify=s4_classify("reply", 0.9, asks_offer=True),
                    extract=s4_extract(),
                    draft=s4_draft(
                        "Happy to put a written number together - it comes from recent sales "
                        "nearby and the condition of the house, so I need a little from you "
                        "first: what has you thinking about selling, and how would you describe "
                        "the overall condition?",
                        "motivation",
                        "condition",
                    ),
                    qa=qa_approve(0.9),
                ),
            ),
            SellerMessage(
                trigger="touch",
                index=1,
                delay_hours=30,
                body="Hello? Still waiting on a number from you people.",
                match="Still waiting on a number",
                model=S4Script(
                    classify=s4_classify("reply", 0.88, asks_offer=True),
                    extract=s4_extract(),
                    draft=s4_draft(
                        "Sorry for the wait. A written number comes from recent sales and the "
                        "house itself, so two quick things first: what has you thinking about "
                        "selling, and how would you describe the overall condition?",
                        "motivation",
                        "condition",
                    ),
                    qa=qa_approve(0.9),
                ),
            ),
        ),
        covers=("POL-06", "POL-09", "SEL-07", "INV-18"),
        expect={
            "lead_found": True,
            "sequence_status": "cancelled",
            "cancel_reason": "reply",
            "touches_sent": 1,
            "inbound_rows": 2,
            "s4_replies": 2,
            "s4_blocked": 0,  # the 2 h reply should be answered, not dropped (GAP-S08)
            "s4_replies_sent": 1,
            "s4_replies_auto": 0,
            "s4_sensitive": 2,
            "s4_queued": 1,
            "intake_complete": False,
            "offer_status": None,
        },
        known_defects={GAP_S08: ("s4_blocked",)},
    ),
    # -- web form leads -------------------------------------------------------------------------
    SellerScenario(
        id="web_form_full_to_close",
        title=(
            "full-info web form with a photo -> A7 offer -> seller accepts -> human PSA + "
            "signature -> S6 blast -> buyer picked -> deposit -> closed with the realised fee"
        ),
        source="web_form",
        owner="Pat Doe",
        address_line1="1204 Oak Hollow Dr",
        postal_code="77008",
        form=WebForm(
            at_hours=1,
            address_line1="1204 Oak Hollow Dr",
            postal_code="77008",
            name="Pat Doe",
            email="pat.doe@example.test",
            motivation="relocating for work",
            condition_class="Medium",
            disclosed_issues="roof",
            timeline="asap",
            asking_price="100000",
            photos=1,
        ),
        messages=(
            SellerMessage(
                trigger="offer_sent",
                delay_hours=20,
                body="Yes, I accept. Please send the paperwork.",
                match="I accept",
                model=S4Script(classify=s4_classify("reply", 0.9), extract=s4_extract()),
                negotiation=NegotiationMove("accept"),
            ),
        ),
        deal_plan="close",
        covers=("SEL-12", "SEL-08", "A7-01", "A7-09", "SEL-11", "SEL-10", "BB-11", "POL-03j"),
        expect={
            "lead_found": True,
            "lead_source": "web_form",
            "parcel_verified": True,
            "sequence_status": None,
            "inbound_rows": 2,
            "classifications": ["interested", "interested"],
            "s4_replies": 0,
            "intake_complete": True,
            "photos": 1,
            "offer_status": "accepted",
            "arv_confidence": "High",
            "gates_failed": [],
            "offer_sent": True,
            "offer_auto": False,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
            "contract_requested": True,
            "negotiation_outcome": "accepted",
            "contract_status": "closed",
            "blast_recipients": 3,
            "buyer_selected": True,
            "deposit_recorded": True,
            "realized_fee": "10000.00",
            "disposition_outcome": "closed",
            "lead_status": "closed",
        },
        expect_by_level={
            AutonomyLevel.L0: {"blast_auto": 0},
            AutonomyLevel.L1: {"blast_auto": 3},
        },
        known_defects={GAP_S03: ("gates_failed",), GAP_S11: ("lead_status",)},
    ),
    SellerScenario(
        id="web_form_three_way_conflict",
        title="web form on the 3-way sqft conflict subject: V1 escalates, Low confidence, human",
        source="web_form",
        owner="Lee Conflict",
        address_line1="61 Elm Loop",
        postal_code="77022",
        form=WebForm(
            at_hours=2,
            address_line1="61 Elm Loop",
            postal_code="77022",
            name="Lee Conflict",
            email="lee.conflict@example.test",
            motivation="downsizing",
            condition_class="Medium",
            timeline="within_90_days",
            photos=1,
        ),
        covers=("A7-12", "A7-04", "A7-08"),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "offer_status": "expired",
            "arv_confidence": "Low",
            "gates_failed": ["arv_confidence", "no_open_flags"],
            "v1_result": "escalate",
            "offer_sent": True,
            "offer_auto": False,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
        },
    ),
    SellerScenario(
        id="web_form_dirty_comps",
        title=(
            "web form on the dirty-comps subject: V3 removes four comps, V4 bumps Medium -> Heavy "
            "with the Houston foundation contingency, the 35%-of-ARV gate fails -> human"
        ),
        source="web_form",
        owner="Sam Cedar",
        address_line1="930 Cedar Bend",
        postal_code="77055",
        form=WebForm(
            at_hours=3,
            address_line1="930 Cedar Bend",
            postal_code="77055",
            name="Sam Cedar",
            email="sam.cedar@example.test",
            motivation="behind on taxes",
            condition_class="Medium",
            timeline="asap",
            photos=1,
        ),
        covers=("A7-14", "A7-15", "A7-08"),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "offer_status": "expired",
            "condition_class_final": "Heavy",
            "comps_rejected": 4,
            "gates_failed": ["min_pct_of_arv", "no_open_flags"],
            "offer_sent": True,
            "offer_auto": False,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
        },
    ),
    SellerScenario(
        id="web_form_avm_gap",
        title="web form on the AVM-gap subject: V2 re-runs comps at tier 2 and passes",
        source="web_form",
        owner="Ana Birch",
        address_line1="2201 Birch St",
        postal_code="77007",
        form=WebForm(
            at_hours=4,
            address_line1="2201 Birch St",
            postal_code="77007",
            name="Ana Birch",
            email="ana.birch@example.test",
            motivation="divorce",
            condition_class="Light",
            timeline="within_30_days",
            photos=2,
        ),
        covers=("A7-13",),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "offer_status": "expired",
            "v2_results": ["adjusted", "adjusted"],  # re-run at tier 2, 7.4% gap recorded
            "comp_tier": 2,
            "gates_failed": [],
            "offer_sent": True,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
        },
        expect_by_level={
            AutonomyLevel.L0: {"offer_auto": False},
            AutonomyLevel.L1: {"offer_auto": True},
        },
        known_defects={GAP_S03: ("gates_failed", "offer_auto")},
    ),
    SellerScenario(
        id="web_form_pre_send_drift",
        title=(
            "web form on the drift subject: three sales recorded between draft and send move the "
            "ARV > 5%, V6 recomputes MAO, offer, V5 and the draft"
        ),
        source="web_form",
        owner="Will Willow",
        address_line1="717 Willow Way",
        postal_code="77009",
        form=WebForm(
            at_hours=5,
            address_line1="717 Willow Way",
            postal_code="77009",
            name="Will Willow",
            email="will.willow@example.test",
            motivation="job transfer",
            condition_class="Medium",
            timeline="flexible",
            photos=1,
        ),
        inject_drift_comps=True,
        covers=("A7-17", "A7-16"),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "offer_status": "expired",
            "v6_results": ["adjusted", "pass"],
            "v5_results": ["pass", "pass"],
            "offer_changed_by_v6": True,
            "offer_sent": True,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5", "V6"],
        },
        expect_by_level={
            AutonomyLevel.L0: {"gates_failed": [], "offer_auto": False},
            AutonomyLevel.L1: {"gates_failed": [], "offer_auto": True},
        },
        known_defects={GAP_S03: ("gates_failed", "offer_auto")},
    ),
    SellerScenario(
        id="web_form_v5_mismatch",
        title=(
            "V5 independent recompute disagrees with the engine (bug injected): offer blocked "
            "before QA, alert raised, nothing sent"
        ),
        source="web_form",
        owner="Vera Five",
        address_line1="1300 Oak Hollow Dr",
        postal_code="77008",
        alias=CLEAN,
        form=WebForm(
            at_hours=6,
            address_line1="1300 Oak Hollow Dr",
            postal_code="77008",
            name="Vera Five",
            email="vera.five@example.test",
            motivation="estate sale handled by the executor",
            condition_class="Medium",
            timeline="asap",
            photos=1,
        ),
        inject_v5_mismatch=True,
        covers=("A7-16",),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "offer_status": "blocked",
            "blocked_reason_prefix": "V5 recompute mismatch",
            "v5_results": ["fail"],
            "alert_raised": True,
            "offer_sent": False,
            "verification_passes": ["V1", "V2", "V3", "V4", "V5"],
        },
    ),
    SellerScenario(
        id="web_form_photos_missing",
        title="web form without photos on a clean subject: confidence drops one level (SEL-06)",
        source="web_form",
        owner="Noah Photo",
        address_line1="1302 Oak Hollow Dr",
        postal_code="77008",
        alias=CLEAN,
        form=WebForm(
            at_hours=7,
            address_line1="1302 Oak Hollow Dr",
            postal_code="77008",
            name="Noah Photo",
            email="noah.photo@example.test",
            motivation="moving closer to family",
            condition_class="Medium",
            disclosed_issues="roof",
            timeline="within_30_days",
            asking_price="100000",
            photos=0,
        ),
        covers=("SEL-06", "A7-04"),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "photos": 0,
            "arv_confidence": "Medium",
            "gates_failed": [],
            "offer_status": "expired",
            "offer_sent": True,
            "offer_auto": False,
        },
        known_defects={GAP_S03: ("gates_failed",)},
    ),
    SellerScenario(
        id="web_form_probate_without_authority",
        title="inherited house in probate with no stated authority: gate fails -> human",
        source="web_form",
        owner="Pat Probate",
        address_line1="1304 Oak Hollow Dr",
        postal_code="77008",
        alias=CLEAN,
        form=WebForm(
            at_hours=8,
            address_line1="1304 Oak Hollow Dr",
            postal_code="77008",
            name="Pat Probate",
            email="pat.probate@example.test",
            motivation="inherited the house when my mother passed away; it is still in probate",
            condition_class="Medium",
            timeline="flexible",
            photos=1,
        ),
        covers=("A7-08",),
        expect={
            "lead_found": True,
            "intake_complete": True,
            "open_flags": ["co_owner_missing", "probate_without_authority"],
            "gates_failed": ["no_open_flags"],
            "offer_status": "expired",
            "offer_sent": True,
            "offer_auto": False,
        },
    ),
    SellerScenario(
        id="web_form_same_parcel_merges",
        title=(
            "a second web form for an S1 lead's address (same owner) merges into the existing "
            "lead: one lead per parcel, one intake"
        ),
        source="web_form",
        owner=_RAMIREZ,
        address_line1="1420 Pecan Grove Dr",
        postal_code="77009",
        form=WebForm(
            at_hours=45 * 24,
            address_line1="1420 Pecan Grove Dr",
            postal_code="77009",
            name="Dolores Ramirez",
            email="dolores.ramirez@example.test",
            photos=0,
        ),
        covers=("SEL-02", "SEL-12"),
        expect={
            "lead_found": True,
            "leads_for_address": 1,
            "form_lead_created": False,
            "intakes": 1,
            "s1_rows_merged": 2,
        },
    ),
    # -- cohort checks ----------------------------------------------------------------------------
    SellerScenario(
        id="market_not_active_refused",
        title="S1 refuses a Watch market (Columbus): nothing pulled, nothing queued",
        source="s1",
        owner=None,
        covers=("SEL-01", "WF-10", "M1-11"),
        expect={"refused_reason": "market_not_active", "leads": 0},
    ),
)

SCENARIO_IDS: tuple[str, ...] = tuple(s.id for s in SELLER_SCENARIOS)
PERSONA_SCENARIOS: tuple[SellerScenario, ...] = tuple(
    s for s in SELLER_SCENARIOS if not s.is_cohort_check
)


def scenario(scenario_id: str) -> SellerScenario:
    for s in SELLER_SCENARIOS:
        if s.id == scenario_id:
            return s
    msg = f"no seller scenario {scenario_id!r}"
    raise KeyError(msg)


__all__ = [
    "CLEAN",
    "GAP_S03",
    "GAP_S04",
    "GAP_S05",
    "GAP_S08",
    "GAP_S09",
    "GAP_S11",
    "GAP_S12",
    "PERSONA_SCENARIOS",
    "SCENARIO_IDS",
    "SELLER_LEVEL_ACTIONS",
    "SELLER_MARKET",
    "SELLER_MARKET_STATE",
    "SELLER_SCENARIOS",
    "SELLER_TEMPLATE_QA",
    "SQFT_GAP",
    "TIER2",
    "WATCH_MARKET",
    "DealPlan",
    "LeadSource",
    "NegotiationMove",
    "PropertyAlias",
    "S4Script",
    "SeedLead",
    "SellerMessage",
    "SellerScenario",
    "Trigger",
    "WebForm",
    "qa_approve",
    "s4_classify",
    "s4_draft",
    "s4_extract",
    "scenario",
]
