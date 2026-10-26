"""The synthetic pilot's investor personas (REQUIREMENTS.md BO-09, INV-22, INV-23, INV-24).

Every scenario binds to one investor the mock data provider's fixtures already produce for the
pilot markets (Houston, TX and Columbus, OH; no fixtures are added here) and scripts what that
investor does after each outreach touch: nothing, a reply, an opt-out, an out-of-office, a
bounce. For each scripted inbound message the scenario also scripts what the *mock* model must
answer for A4 (classify / extract / draft) and A5 (QA), so the pipeline is exercised with
realistic structured outputs rather than schema placeholders, and it states the expected
outcome: classification, buy-box versions, whether a reply is drafted, queued, blocked or
auto-sent.

``Scenario.expected(level, approve_everything)`` turns the script into the row counts and
states the report checks, so the same catalogue grades an L0 run and an L1 run.

Persona -> fixture investor mapping (scores from ``docs/agents/discovery.md`` and the Columbus
seeded volume):

| id                                  | investor                         | market   | score |
|-------------------------------------|----------------------------------|----------|-------|
| priority_no_reply                   | Bayou Capital Partners LLC       | Houston  | 88    |
| l1_auto_send_then_price_talk        | Lone Star Flips LLC              | Houston  | 75    |
| partial_then_correction             | Heights Holdings LLC             | Houston  | 71    |
| two_buy_boxes                       | Keller Realty Investments LLC    | Houston  | 64    |
| not_interested                      | Fast Close Houston LLC           | Houston  | 61    |
| prospect_interested_full_buy_box    | Maria Hernandez                  | Houston  | 58    |
| no_contact_found                    | Gulf Coast Rentals LLC           | Houston  | 76    |
| bounce_after_touch_1                | Harbor Capital LP 6              | Columbus | 84    |
| opt_out_after_touch_2               | Summit Equity LLC 5              | Columbus | 77    |
| question                            | Prairie Property LLC 9           | Columbus | 68    |
| not_now_with_date                   | Summit Holdings Inc 7            | Columbus | 67    |
| ooo_then_real_reply                 | Harbor Assets Inc 2              | Columbus | 66    |
| sarcasm                             | Blue Capital LLC 0               | Columbus | 65    |
| quoted_old_text                     | Pine Property Trust 3            | Columbus | 62    |
| suppressed_before_start             | River Holdings LLC 4             | Columbus | 56    |
| priority_before_prospect            | (cohort check, no persona)       | both     |       |
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from wholesaling.db.models.enums import MessageClassification, QaVerdict
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

PILOT_MARKETS: tuple[str, ...] = ("Houston", "Columbus")
"""Default markets. Houston is the hand-designed fixture; Columbus has the most emailable
seeded investors (8), which together give 15 personas with a contact and one without."""

PRIORITY_SCORE = 75  # INV-04: >= 75 is the priority tier, >= 50 a prospect
QA_THRESHOLD = 0.85  # OPN-10 default the policy engine applies at L1

ReplyKind = Literal["reply", "bounce"]
InvestorTier = Literal["priority", "prospect"]


# --------------------------------------------------------------------------------------------
# Model script helpers (what the *mock* model returns for one inbound message)
# --------------------------------------------------------------------------------------------


def classify(
    label: str,
    confidence: float,
    rationale: str,
    *,
    reconnect_date: str | None = None,
    is_auto_reply: bool = False,
) -> dict[str, Any]:
    return {
        "classification": label,
        "confidence": confidence,
        "rationale": rationale,
        "is_auto_reply": is_auto_reply,
        "reconnect_date": reconnect_date,
    }


def box(key: str = "default", **fields: Any) -> dict[str, Any]:
    """One ``BUY_BOX_ITEM_SCHEMA`` item; unspecified criteria stay ``null`` (never invented)."""
    base: dict[str, Any] = {
        "buy_box_key": key,
        "geography": None,
        "asset_types": None,
        "price_min": None,
        "price_max": None,
        "condition": None,
        "strategy": None,
        "financing": None,
        "closing_timeline": None,
        "other_criteria": [],
        "corrections": [],
        "raw_text": None,
    }
    base.update(fields)
    return base


def geography(**parts: list[str]) -> dict[str, Any]:
    return {
        "markets": parts.get("markets"),
        "cities": parts.get("cities"),
        "zips": parts.get("zips"),
        "neighborhoods": parts.get("neighborhoods"),
    }


def extraction(*boxes: dict[str, Any], rationale: str = "stated criteria only") -> dict[str, Any]:
    return {"buy_boxes": list(boxes), "rationale": rationale}


def draft(body: str, *asks: str, sensitive: bool = False, subject: str = "") -> dict[str, Any]:
    return {
        "subject": subject,
        "body": body,
        "asks_for": list(asks),
        "contains_sensitive": sensitive,
    }


def qa(confidence: float = 0.92, verdict: str = "approve", rationale: str = "") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "flags": [],
        "rewrite_body": None,
        "rationale": rationale or "Accurate to the thread, no claims, asks only for unknowns.",
    }


@dataclass(frozen=True, slots=True)
class ModelScript:
    """Scripted structured outputs for one inbound message. ``None`` = the pilot expects no
    model call for that task (auto-reply short-cut, no buy box, no reply warranted)."""

    classify: dict[str, Any] | None = None
    extract: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None


# --------------------------------------------------------------------------------------------
# Scripted inbound messages and their expectations
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplyExpectation:
    classification: str | None  # MessageClassification value; None for a bounce
    is_auto_reply: bool = False
    quoted_text_stripped: bool = False
    reconnect_date: str | None = None
    buy_box_versions: Mapping[str, int] = field(default_factory=dict)  # key -> version produced
    corrections: int = 0
    draft: bool = False  # a reply draft row exists afterwards
    sensitive: bool = False  # A4 flagged price/contract/legal content (POL-03c)
    qa_verdict: str | None = None
    qa_confidence: float | None = None
    signal: str | None = None  # reply | opt_out | bounce signalled to the sequence
    suppression_reason: str | None = None  # opt_out | bounce
    model_calls: int = 0  # agent_runs rows this message must produce

    def auto_sends_at(self, level: AutonomyLevel) -> bool:
        """POL-03c: at L1 a QA-approved, confident, non-sensitive reply sends itself."""
        if not self.draft or level is AutonomyLevel.L0:
            return False
        if level is AutonomyLevel.L2:
            return self.qa_verdict != QaVerdict.BLOCK.value
        return (
            self.qa_verdict == QaVerdict.APPROVE.value
            and (self.qa_confidence or 0.0) >= QA_THRESHOLD
            and not self.sensitive
        )


@dataclass(frozen=True, slots=True)
class ScriptedReply:
    """One inbound message the persona sends ``delay_hours`` after touch ``after_touch`` went
    out (touch 1 = the initial). ``match`` is a phrase from the body that survives quote
    stripping; the pilot keys the mock model's scripts on it."""

    after_touch: int
    delay_hours: float
    body: str
    match: str
    expect: ReplyExpectation
    model: ModelScript = field(default_factory=ModelScript)
    subject: str | None = None  # None -> "RE: <our subject>"
    headers: Mapping[str, str] = field(default_factory=dict)
    kind: ReplyKind = "reply"
    same_conversation: bool = True
    from_address: str | None = None  # None -> the persona's contact email


# --------------------------------------------------------------------------------------------
# Scenario
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expected:
    """What the database must show for one persona when the pilot is over."""

    sequence_started: bool
    sequence_status: str | None  # completed | cancelled | active | None (never started)
    cancel_reason: str | None
    touches_sent: int
    auto_touches: int  # touches that went out without a human (L1 auto-send path)
    inbound_rows: int
    classifications: tuple[str, ...]
    reply_drafts: int
    replies_sent: int
    replies_auto_sent: int
    queued_items: int  # drafts that waited in the review queue (sequence steps + replies)
    buy_box_versions: Mapping[str, int]  # key -> current version
    corrections: int
    suppression_reason: str | None
    model_calls: int
    one_conversation: bool
    tier: InvestorTier | None


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    market: str | None  # market name (``markets.name``); None for a cohort check
    investor: str | None  # ``investors.canonical_name``; None for a cohort check
    tier: InvestorTier | None
    has_contact: bool = True
    replies: tuple[ScriptedReply, ...] = ()
    suppress_before_start: bool = False
    #: Policy levels forced around this persona's own workflow starts (and restored once the
    #: workflow blocks). Used by the L1 persona so one investor exercises the auto-send path
    #: while the rest of the pilot stays at L0.
    policy_scope: Mapping[PolicyAction, AutonomyLevel] = field(default_factory=dict)
    #: Record an A5 ``approve`` on this persona's sequence drafts before the policy decision
    #: (the A3 path has no QA call of its own; see docs/pilot.md gap list).
    qa_standin_for_sequence: bool = False
    #: Requirement ids this scenario is evidence for.
    covers: tuple[str, ...] = ()
    #: Defects in the system under test that this scenario is currently expected to trip. A
    #: scenario that fails *only* on checks listed here is reported as ``known_defect``.
    known_defects: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_cohort_check(self) -> bool:
        return self.investor is None

    def level_for(self, action: PolicyAction, global_level: AutonomyLevel) -> AutonomyLevel:
        scoped = self.policy_scope.get(action)
        if scoped is None:
            return global_level
        return max(scoped, global_level, key=_level_rank)

    def expected(
        self,
        level: AutonomyLevel,
        *,
        approve_everything: bool = True,
        qa_standin: bool = True,
    ) -> Expected:
        """Derive the end state from the script for a run at ``level`` (the global level).

        ``qa_standin=False`` (Temporal mode) means nobody can write the A5 approval the L1
        initial needs, so even the L1 persona's initial queues."""
        if not self.has_contact:
            return Expected(
                sequence_started=False,
                sequence_status=None,
                cancel_reason=None,
                touches_sent=0,
                auto_touches=0,
                inbound_rows=0,
                classifications=(),
                reply_drafts=0,
                replies_sent=0,
                replies_auto_sent=0,
                queued_items=0,
                buy_box_versions={},
                corrections=0,
                suppression_reason=None,
                model_calls=0,
                one_conversation=True,
                tier=self.tier,
            )
        if self.suppress_before_start:
            return Expected(
                sequence_started=True,
                sequence_status="cancelled",
                cancel_reason="suppressed",
                touches_sent=0,
                auto_touches=0,
                inbound_rows=0,
                classifications=(),
                reply_drafts=0,
                replies_sent=0,
                replies_auto_sent=0,
                queued_items=0,
                buy_box_versions={},
                corrections=0,
                suppression_reason="manual",
                model_calls=0,
                one_conversation=True,
                tier=self.tier,
            )

        # Touches: the sequence runs until the first cancelling inbound message, or parks on
        # the first queued step when the operator approves nothing.
        cancel_reason: str | None = None
        cancel_after_touch: int | None = None
        for r in self.replies:
            if r.expect.signal is not None:
                cancel_after_touch = r.after_touch
                cancel_reason = r.expect.signal
                break
        initial_level = self.level_for(PolicyAction.INVESTOR_INITIAL_OUTREACH, level)
        auto_initial = (
            initial_level is not AutonomyLevel.L0 and self.qa_standin_for_sequence and qa_standin
        ) or initial_level is AutonomyLevel.L2
        followups_auto = self.level_for(PolicyAction.INVESTOR_FOLLOWUP, level) is AutonomyLevel.L2
        touches = 0
        auto_touches = 0
        queued = 0
        parked = False
        for step in range(cancel_after_touch or 4):
            auto = auto_initial if step == 0 else followups_auto
            if auto:
                touches += 1
                auto_touches += 1
            elif approve_everything:
                touches += 1
                queued += 1
            else:
                queued += 1
                parked = True
                break
        if cancel_after_touch is not None and touches >= cancel_after_touch:
            status = "cancelled"
        elif parked:
            status = "active"
            cancel_reason = None
        else:
            status = "completed"
            cancel_reason = None

        # Inbound side.
        classifications: list[str] = []
        drafts = 0
        sent = 0
        auto_sent = 0
        boxes: dict[str, int] = {}
        corrections = 0
        suppression: str | None = None
        model_calls = 0
        reply_level = self.level_for(PolicyAction.INVESTOR_REPLY, level)
        inbound = 0
        for r in self.replies:
            if r.after_touch > touches:
                break  # the touch it answers never went out
            inbound += 1
            e = r.expect
            if e.classification is not None:
                classifications.append(e.classification)
            model_calls += e.model_calls
            corrections += e.corrections
            for key, version in e.buy_box_versions.items():
                boxes[key] = max(boxes.get(key, 0), version)
            if e.suppression_reason:
                suppression = e.suppression_reason
            if e.draft:
                drafts += 1
                if e.qa_verdict == QaVerdict.BLOCK.value:
                    continue
                if e.auto_sends_at(reply_level):
                    auto_sent += 1
                    sent += 1
                else:
                    queued += 1
                    if approve_everything:
                        sent += 1
        if self.suppress_before_start:
            suppression = "manual"
        return Expected(
            sequence_started=True,
            sequence_status=status,
            cancel_reason=cancel_reason,
            touches_sent=touches,
            auto_touches=auto_touches,
            inbound_rows=inbound,
            classifications=tuple(classifications),
            reply_drafts=drafts,
            replies_sent=sent,
            replies_auto_sent=auto_sent,
            queued_items=queued,
            buy_box_versions=boxes,
            corrections=corrections,
            suppression_reason=suppression,
            model_calls=model_calls,
            one_conversation=True,
            tier=self.tier,
        )


def _level_rank(level: AutonomyLevel) -> int:
    return [AutonomyLevel.L0, AutonomyLevel.L1, AutonomyLevel.L2].index(level)


# --------------------------------------------------------------------------------------------
# Shared text
# --------------------------------------------------------------------------------------------

OOO_HEADERS = {"auto-submitted": "auto-replied", "x-auto-response-suppress": "All"}
# ``model_calls`` counts: classify (1) + extract (1 when a buy box class) + draft (1) + qa (1).
_INTERESTED_CALLS = 4
_REPLY_CALLS = 3  # classify + draft + qa
_NO_REPLY_CALLS = 1  # classify only

C = MessageClassification


def _reply_exp(
    classification: C,
    *,
    model_calls: int,
    draft: bool = True,
    boxes: Mapping[str, int] | None = None,
    corrections: int = 0,
    sensitive: bool = False,
    confidence: float = 0.92,
    reconnect_date: str | None = None,
    stripped: bool = False,
) -> ReplyExpectation:
    return ReplyExpectation(
        classification=classification.value,
        quoted_text_stripped=stripped,
        reconnect_date=reconnect_date,
        buy_box_versions=dict(boxes or {}),
        corrections=corrections,
        draft=draft,
        sensitive=sensitive,
        qa_verdict=QaVerdict.APPROVE.value if draft else None,
        qa_confidence=confidence if draft else None,
        signal="reply",
        model_calls=model_calls,
    )


# --------------------------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------------------------

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="priority_no_reply",
        title="Priority investor never replies: four touches in one conversation, then done",
        market="Houston",
        investor="Bayou Capital Partners LLC",
        tier="priority",
        covers=("INV-10", "INV-23", "INV-04"),
    ),
    Scenario(
        id="l1_auto_send_then_price_talk",
        title=(
            "L1 persona: QA-approved initial auto-sends, a clean reply auto-sends at L1, then "
            "price/contract talk queues at L1"
        ),
        market="Houston",
        investor="Lone Star Flips LLC",
        tier="priority",
        policy_scope={
            PolicyAction.INVESTOR_INITIAL_OUTREACH: AutonomyLevel.L1,
            PolicyAction.INVESTOR_REPLY: AutonomyLevel.L1,
        },
        qa_standin_for_sequence=True,
        covers=("POL-01", "POL-03a", "POL-03c", "POL-09", "XC-01"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=26,
                body=(
                    "Interested. We flip in Spring and Humble, single family only, up to 200k "
                    "all-in, cash. Send what fits.\n\n-- Dale, Lone Star Flips"
                ),
                match="We flip in Spring and Humble",
                model=ModelScript(
                    classify=classify(
                        "buy_box_supplied", 0.93, "States areas, asset type, price cap, cash."
                    ),
                    extract=extraction(
                        box(
                            geography=geography(markets=["Houston"], cities=["Spring", "Humble"]),
                            asset_types=["sfr"],
                            price_max=200000,
                            financing="cash",
                            raw_text="flip in Spring and Humble, SFR only, up to 200k all-in, cash",
                        )
                    ),
                    draft=draft(
                        "Thanks Dale. Spring and Humble, single family, cash buyer, noted. Two "
                        "quick questions so we only send what fits: what condition are you "
                        "willing to take on, and how quickly can you typically close once you "
                        "like a property?",
                        "condition",
                        "closing_timeline",
                    ),
                    qa=qa(0.95),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"default": 1},
                    confidence=0.95,
                ),
            ),
            ScriptedReply(
                after_touch=1,
                delay_hours=52,
                body=(
                    "Cosmetic to medium rehab, 21 days close. Question for you: what kind of "
                    "price would you be at on a typical deal, and do you assign the contract or "
                    "double close?"
                ),
                match="do you assign the contract or double close",
                model=ModelScript(
                    classify=classify(
                        "question", 0.9, "Asks about pricing and contract structure."
                    ),
                    draft=draft(
                        "Good question. Pricing and contract terms depend on the property, so "
                        "our team would rather walk you through how we structure each deal on a "
                        "quick call than generalise here. In the meantime, cosmetic-to-medium "
                        "rehab and a 21-day close are noted.",
                        sensitive=True,
                    ),
                    qa=qa(0.9),
                ),
                expect=_reply_exp(
                    C.QUESTION, model_calls=_REPLY_CALLS, sensitive=True, confidence=0.9
                ),
            ),
        ),
    ),
    Scenario(
        id="partial_then_correction",
        title="Partial buy box after touch 1, then a correction creates version 2 with history",
        market="Houston",
        investor="Heights Holdings LLC",
        tier="prospect",
        covers=("INV-14", "INV-21", "BB-02"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=30,
                body=(
                    "We buy in the Heights and Oak Forest, mostly single family and the odd "
                    "duplex, up to 250k. Send what you have."
                ),
                match="Heights and Oak Forest",
                model=ModelScript(
                    classify=classify(
                        "buy_box_supplied", 0.9, "Neighbourhoods, asset types and a price cap."
                    ),
                    extract=extraction(
                        box(
                            geography=geography(
                                markets=["Houston"], neighborhoods=["Heights", "Oak Forest"]
                            ),
                            asset_types=["sfr", "duplex"],
                            price_max=250000,
                            raw_text="Heights and Oak Forest, SFR and duplex, up to 250k",
                        )
                    ),
                    draft=draft(
                        "Got it: the Heights and Oak Forest, single family and duplexes. So we "
                        "send the right ones, what condition are you comfortable taking on, and "
                        "are you closing with cash or hard money?",
                        "condition",
                        "financing",
                    ),
                    qa=qa(0.9),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"default": 1},
                    confidence=0.9,
                ),
            ),
            ScriptedReply(
                after_touch=1,
                delay_hours=78,
                body=(
                    "Correction, I said 150k max, not 250k. Light cosmetic only and we pay cash."
                ),
                match="I said 150k max, not 250k",
                model=ModelScript(
                    classify=classify(
                        "buy_box_supplied", 0.92, "Corrects the price cap, adds condition/cash."
                    ),
                    extract=extraction(
                        box(
                            price_max=150000,
                            condition="light cosmetic",
                            financing="cash",
                            corrections=[{"field": "price_max", "old": "250000", "new": "150000"}],
                            raw_text="150k max, light cosmetic, cash",
                        )
                    ),
                    draft=draft(
                        "Thanks for the correction, the lower cap is what we have on file now, "
                        "light cosmetic only, cash. Last thing: once you like a property, how "
                        "quickly can you close?",
                        "closing_timeline",
                    ),
                    qa=qa(0.9),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"default": 2},
                    corrections=1,
                    confidence=0.9,
                ),
            ),
        ),
    ),
    Scenario(
        id="two_buy_boxes",
        title="Two buy boxes in one email become two keyed version-1 rows",
        market="Houston",
        investor="Keller Realty Investments LLC",
        tier="prospect",
        covers=("BB-03", "INV-14", "INV-03"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=28,
                body=(
                    "Two things we buy. (1) Flips in Katy and Cypress under 300k that need a "
                    "full rehab. (2) Rentals in Pasadena and Deer Park under 180k, turnkey or "
                    "light work."
                ),
                match="Two things we buy",
                model=ModelScript(
                    classify=classify(
                        "buy_box_supplied", 0.94, "Two distinct criteria sets stated."
                    ),
                    extract=extraction(
                        box(
                            "flips",
                            geography=geography(markets=["Houston"], cities=["Katy", "Cypress"]),
                            asset_types=["sfr"],
                            price_max=300000,
                            condition="full rehab",
                            strategy="flip",
                            raw_text="flips in Katy and Cypress under 300k, full rehab",
                        ),
                        box(
                            "rentals",
                            geography=geography(
                                markets=["Houston"], cities=["Pasadena", "Deer Park"]
                            ),
                            asset_types=["sfr"],
                            price_max=180000,
                            condition="turnkey or light",
                            strategy="rental",
                            raw_text="rentals in Pasadena and Deer Park under 180k",
                        ),
                    ),
                    draft=draft(
                        "Thanks, both buckets are on file: Katy and Cypress flips needing a "
                        "full rehab, and Pasadena and Deer Park rentals that are turnkey or "
                        "light. For both, are you a cash buyer, and how fast can you close?",
                        "financing",
                        "closing_timeline",
                    ),
                    qa=qa(0.91),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"flips": 1, "rentals": 1},
                    confidence=0.91,
                ),
            ),
        ),
    ),
    Scenario(
        id="not_interested",
        title="Not interested after touch 2: no reply drafted, remaining touches cancelled",
        market="Houston",
        investor="Fast Close Houston LLC",
        tier="prospect",
        covers=("INV-10", "INV-13"),
        replies=(
            ScriptedReply(
                after_touch=2,
                delay_hours=26,
                body="No thanks, we source our own and we are not adding wholesalers right now.",
                match="we source our own",
                model=ModelScript(
                    classify=classify("not_interested", 0.95, "Clear decline, no opt-out wording.")
                ),
                expect=_reply_exp(C.NOT_INTERESTED, model_calls=_NO_REPLY_CALLS, draft=False),
            ),
        ),
    ),
    Scenario(
        id="prospect_interested_full_buy_box",
        title="Prospect-tier individual replies interested with a complete buy box after touch 1",
        market="Houston",
        investor="Maria Hernandez",
        tier="prospect",
        covers=("INV-14", "INV-21", "INV-04"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=26,
                body=(
                    "Hi Aaron, yes I am buying. Looking for 3/2 single family in Harris County, "
                    "zips 77084 and 77095, up to 220k purchase, cosmetic rehabs only, cash, and "
                    "I can close in 14 days. Thanks, Maria"
                ),
                match="zips 77084 and 77095",
                model=ModelScript(
                    classify=classify(
                        "buy_box_supplied", 0.96, "Complete criteria set with timeline."
                    ),
                    extract=extraction(
                        box(
                            geography=geography(markets=["Houston"], zips=["77084", "77095"]),
                            asset_types=["sfr"],
                            price_max=220000,
                            condition="cosmetic",
                            financing="cash",
                            closing_timeline="14 days",
                            raw_text="3/2 SFR, 77084/77095, up to 220k, cosmetic, cash, 14 days",
                        )
                    ),
                    draft=draft(
                        "Thanks Maria, that is clear and complete: 3/2 single family in 77084 and "
                        "77095, cosmetic work only, cash, closing within two weeks. Nothing else "
                        "we need from you. When something fits we will send it over with photos "
                        "and the numbers we have."
                    ),
                    qa=qa(0.94),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"default": 1},
                    confidence=0.94,
                ),
            ),
        ),
    ),
    Scenario(
        id="no_contact_found",
        title="Priority investor with no email in enrichment: no sequence can start",
        market="Houston",
        investor="Gulf Coast Rentals LLC",
        tier="priority",
        has_contact=False,
        covers=("INV-08", "WF-02"),
    ),
    Scenario(
        id="bounce_after_touch_1",
        title="NDR after touch 1: bounce suppression, sequence cancelled, nothing drafted",
        market="Columbus",
        investor="Harbor Capital LP 6",
        tier="priority",
        covers=("INT-25", "INV-12", "WF-21"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=1,
                kind="bounce",
                same_conversation=False,
                from_address="postmaster@harborcapitallp6.example",
                subject="Undeliverable: {subject}",
                body=(
                    "Delivery has failed to these recipients or groups:\n\n"
                    "contact@harborcapitallp6.example\n"
                    "The recipient's mailbox is full and can't accept messages now.\n"
                ),
                match="Delivery has failed to these recipients",
                expect=ReplyExpectation(
                    classification=None,
                    signal="bounce",
                    suppression_reason="bounce",
                    model_calls=0,
                ),
            ),
        ),
    ),
    Scenario(
        id="opt_out_after_touch_2",
        title="'Unsubscribe' after touch 2: suppression at every level, sequence cancelled",
        market="Columbus",
        investor="Summit Equity LLC 5",
        tier="priority",
        covers=("INV-15", "POL-03l", "WF-21"),
        replies=(
            ScriptedReply(
                after_touch=2,
                delay_hours=20,
                subject="unsubscribe",
                body="Unsubscribe. Remove me from this list.",
                match="Remove me from this list",
                model=ModelScript(
                    classify=classify("opt_out", 0.99, "Explicit unsubscribe request.")
                ),
                expect=ReplyExpectation(
                    classification=C.OPT_OUT.value,
                    signal="opt_out",
                    suppression_reason="opt_out",
                    model_calls=_NO_REPLY_CALLS,
                ),
            ),
        ),
    ),
    Scenario(
        id="question",
        title="A question gets a drafted answer; no buy box is invented",
        market="Columbus",
        investor="Prairie Property LLC 9",
        tier="prospect",
        covers=("INV-13", "INV-14", "INV-16"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=40,
                body=(
                    "Where are you finding these, MLS or off-market? And do you work with "
                    "out-of-state buyers?"
                ),
                match="MLS or off-market",
                model=ModelScript(
                    classify=classify("question", 0.9, "Two questions, no criteria."),
                    draft=draft(
                        "Off-market, direct from owners, nothing from the MLS. And yes, "
                        "out-of-state buyers are fine; everything is shared with photos and the "
                        "figures we have so you can decide remotely. If you want to tell us what "
                        "you buy, we will only send what fits.",
                        "geography",
                        "asset_types",
                    ),
                    qa=qa(0.9),
                ),
                expect=_reply_exp(C.QUESTION, model_calls=_REPLY_CALLS, confidence=0.9),
            ),
        ),
    ),
    Scenario(
        id="not_now_with_date",
        title="Not now with a date: reconnect date captured, short acknowledgement drafted",
        market="Columbus",
        investor="Summit Holdings Inc 7",
        tier="prospect",
        covers=("INV-15", "INV-13"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=30,
                body=(
                    "Not right now, we are capped until the new year. Circle back around "
                    "January 15, 2027."
                ),
                match="Circle back around January 15",
                model=ModelScript(
                    classify=classify(
                        "not_now",
                        0.9,
                        "Defers with an explicit date.",
                        reconnect_date="2027-01-15",
                    ),
                    draft=draft(
                        "Understood, thanks for letting us know. We will reach back out around "
                        "the fifteenth of January and leave you alone until then."
                    ),
                    qa=qa(0.93),
                ),
                expect=_reply_exp(
                    C.NOT_NOW,
                    model_calls=_REPLY_CALLS,
                    reconnect_date="2027-01-15",
                    confidence=0.93,
                ),
            ),
        ),
    ),
    Scenario(
        id="ooo_then_real_reply",
        title="Out-of-office never cancels; the real reply after touch 2 does",
        market="Columbus",
        investor="Harbor Assets Inc 2",
        tier="prospect",
        covers=("INV-22", "INV-10", "INV-14"),
        known_defects={
            "GAP-02": (
                "touches_sent",
                "auto_touches",
                "sequence_status",
                "cancel_reason",
                "inbound_rows",
                "classifications",
                "reply_drafts",
                "replies_sent",
                "replies_auto_sent",
                "queued_items",
                "buy_box_versions",
                "model_calls",
            )
        },
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=0.5,
                subject="Automatic reply: {subject}",
                headers=OOO_HEADERS,
                body=(
                    "I am out of the office until November 3 with limited access to email. I "
                    "will respond when I return."
                ),
                match="out of the office until November 3",
                expect=ReplyExpectation(
                    classification=C.AMBIGUOUS.value,
                    is_auto_reply=True,
                    model_calls=0,
                ),
            ),
            ScriptedReply(
                after_touch=2,
                delay_hours=26,
                body=(
                    "Back now. Yes, interested: we hold rentals around Linden and Hilltop, "
                    "under 150k, any condition."
                ),
                match="rentals around Linden and Hilltop",
                model=ModelScript(
                    classify=classify("buy_box_supplied", 0.91, "Areas, price cap, condition."),
                    extract=extraction(
                        box(
                            geography=geography(
                                markets=["Columbus"], neighborhoods=["Linden", "Hilltop"]
                            ),
                            asset_types=["sfr"],
                            price_max=150000,
                            condition="any",
                            strategy="rental",
                            raw_text="rentals around Linden and Hilltop, under 150k, any condition",
                        )
                    ),
                    draft=draft(
                        "Welcome back, and thanks. Linden and Hilltop rentals, any condition, "
                        "noted. Are you buying with cash or financing, and how quickly can you "
                        "close?",
                        "financing",
                        "closing_timeline",
                    ),
                    qa=qa(0.9),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"default": 1},
                    confidence=0.9,
                ),
            ),
        ),
    ),
    Scenario(
        id="sarcasm",
        title="Sarcastic 'sign me up' is a decline, not interest",
        market="Columbus",
        investor="Blue Capital LLC 0",
        tier="prospect",
        covers=("INV-22", "INV-13"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=26,
                body=(
                    "Oh sure, I LOVE getting spam about 'off-market deals'. Sign me up for more "
                    "of this, please."
                ),
                match="Sign me up for more of this",
                model=ModelScript(
                    classify=classify(
                        "not_interested",
                        0.86,
                        "Sarcastic; the literal 'sign me up' is not interest.",
                    )
                ),
                expect=_reply_exp(C.NOT_INTERESTED, model_calls=_NO_REPLY_CALLS, draft=False),
            ),
        ),
    ),
    Scenario(
        id="quoted_old_text",
        title="Reply quoting our email: quoted text stripped before classification",
        market="Columbus",
        investor="Pine Property Trust 3",
        tier="prospect",
        covers=("INV-22", "INV-14"),
        replies=(
            ScriptedReply(
                after_touch=1,
                delay_hours=26,
                body=(
                    "Yes, send me what you have in Clintonville. Single family under 200k, cash "
                    "buyer.\n\n"
                    "-----Original Message-----\n"
                    "From: Aaron <deals@pilot.example>\n"
                    "Sent: Tuesday, October 27, 2026 11:00 AM\n"
                    "To: contact@pinepropertytrust3.example\n"
                    "Subject: {subject}\n\n"
                    "{quoted}"
                ),
                match="send me what you have in Clintonville",
                model=ModelScript(
                    classify=classify("buy_box_supplied", 0.92, "Area, asset type, cap, cash."),
                    extract=extraction(
                        box(
                            geography=geography(
                                markets=["Columbus"], neighborhoods=["Clintonville"]
                            ),
                            asset_types=["sfr"],
                            price_max=200000,
                            financing="cash",
                            raw_text="Clintonville, SFR under 200k, cash",
                        )
                    ),
                    draft=draft(
                        "Will do. Clintonville single family, cash, noted. What condition are "
                        "you willing to take on, and how fast can you close when one fits?",
                        "condition",
                        "closing_timeline",
                    ),
                    qa=qa(0.9),
                ),
                expect=_reply_exp(
                    C.BUY_BOX_SUPPLIED,
                    model_calls=_INTERESTED_CALLS,
                    boxes={"default": 1},
                    stripped=True,
                    confidence=0.9,
                ),
            ),
        ),
    ),
    Scenario(
        id="suppressed_before_start",
        title="Address suppressed before the sequence starts: nothing is ever drafted or sent",
        market="Columbus",
        investor="River Holdings LLC 4",
        tier="prospect",
        suppress_before_start=True,
        covers=("POL-03l", "INV-05", "WF-21"),
    ),
    Scenario(
        id="priority_before_prospect",
        title="Cohort: priority-tier investors enter the outreach queue before prospects",
        market=None,
        investor=None,
        tier=None,
        covers=("INV-04",),
    ),
)

SCENARIOS_BY_ID: dict[str, Scenario] = {s.id: s for s in SCENARIOS}


def scenarios_for_markets(markets: tuple[str, ...] | list[str]) -> list[Scenario]:
    """Persona scenarios bound to one of ``markets`` plus every cohort check."""
    wanted = set(markets)
    return [s for s in SCENARIOS if s.market is None or s.market in wanted]


__all__ = [
    "OOO_HEADERS",
    "PILOT_MARKETS",
    "PRIORITY_SCORE",
    "QA_THRESHOLD",
    "SCENARIOS",
    "SCENARIOS_BY_ID",
    "Expected",
    "ModelScript",
    "ReplyExpectation",
    "Scenario",
    "ScriptedReply",
    "box",
    "classify",
    "draft",
    "extraction",
    "geography",
    "qa",
    "scenarios_for_markets",
]
