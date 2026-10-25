"""ORM models. Importing this package registers every table on ``Base.metadata``.

REQUIREMENTS.md DM-01, DM-05, DM-06.

Twenty tables: markets, investors, contacts, source_evidence, buy_boxes, search_profiles,
seller_leads, intakes, offers, offer_verifications, property_candidates, matches, threads,
messages, sequences, suppression, policies, qa_reviews, agent_runs, audit_events.
"""

from __future__ import annotations

from wholesaling.db.base import Base
from wholesaling.db.models import enums
from wholesaling.db.models.buy_boxes import DEFAULT_BUY_BOX_KEY, BuyBox, SearchProfile
from wholesaling.db.models.deals import Match, PropertyCandidate
from wholesaling.db.models.investors import Contact, Investor, SourceEvidence
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Sequence, Suppression, Thread
from wholesaling.db.models.offers import Offer, OfferVerification
from wholesaling.db.models.ops import AgentRun, AuditEvent, Policy, QaReview
from wholesaling.db.models.sellers import Intake, SellerLead

TABLE_NAMES: frozenset[str] = frozenset(
    {
        "markets",
        "investors",
        "contacts",
        "source_evidence",
        "buy_boxes",
        "search_profiles",
        "seller_leads",
        "intakes",
        "offers",
        "offer_verifications",
        "property_candidates",
        "matches",
        "threads",
        "messages",
        "sequences",
        "suppression",
        "policies",
        "qa_reviews",
        "agent_runs",
        "audit_events",
    }
)

__all__ = [
    "DEFAULT_BUY_BOX_KEY",
    "TABLE_NAMES",
    "AgentRun",
    "AuditEvent",
    "Base",
    "BuyBox",
    "Contact",
    "Intake",
    "Investor",
    "Market",
    "Match",
    "Message",
    "Offer",
    "OfferVerification",
    "Policy",
    "PropertyCandidate",
    "QaReview",
    "SearchProfile",
    "SellerLead",
    "Sequence",
    "SourceEvidence",
    "Suppression",
    "Thread",
    "enums",
]
