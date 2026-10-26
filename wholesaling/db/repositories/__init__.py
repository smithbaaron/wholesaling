"""Async repositories (SQLAlchemy 2 style). Upserts are select-then-write inside the caller's
transaction with unique constraints as the backstop (docs/adr/0002-portable-schema.md)."""

from __future__ import annotations

from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.buy_boxes import BuyBoxRepository, BuyBoxVersionResult
from wholesaling.db.repositories.contracts import (
    ContractRepository,
    HumanActionRequired,
    InvalidContractTransition,
)
from wholesaling.db.repositories.investors import InvestorRepository, InvestorUpsertResult
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.policies import HardFloorViolation, PolicyRepository
from wholesaling.db.repositories.qa_reviews import QaReviewRepository
from wholesaling.db.repositories.review import HumanDecision, QueuedItem, ReviewRepository
from wholesaling.db.repositories.sellers import IntakeFields, SellerRepository
from wholesaling.db.repositories.sequences import SequenceRepository

__all__ = [
    "AuditRepository",
    "BuyBoxRepository",
    "BuyBoxVersionResult",
    "ContractRepository",
    "HardFloorViolation",
    "HumanActionRequired",
    "HumanDecision",
    "IntakeFields",
    "InvalidContractTransition",
    "InvestorRepository",
    "InvestorUpsertResult",
    "MessageRepository",
    "PolicyRepository",
    "QaReviewRepository",
    "QueuedItem",
    "ReviewRepository",
    "SellerRepository",
    "SequenceRepository",
]
