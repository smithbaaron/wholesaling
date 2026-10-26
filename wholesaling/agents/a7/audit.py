"""V3 - comp audit (REQUIREMENTS.md A7-14): model flags non-arm's-length, new build, renovated
flip and lot-value outlier comps; the engine removes flagged comps and recomputes.

Two layers, both recorded:

1. **Deterministic pre-filters** (``prefilter_flags``) where the record itself is conclusive:
   new build when ``year_built >= sale_year - 1``; lot-value outlier when price/sqft is more than
   2.5x the set's median (or the remarks say teardown / land value); renovated flip when the
   prior sale was within 365 days at <= 70% of the resale price, or the remarks say renovated and
   the prior sale is recent; non-arm's-length when buyer and seller share a surname, the price
   is below 60% of the median price/sqft with no market exposure, or the remarks say family /
   estate / quitclaim transfer. These make the mock path meaningful.
2. **Model audit** (``A7CompAuditAgent.audit``, Sonnet via the ``comp_audit`` task kind) over
   the same comps with a strict JSON schema. The model may add flags; it cannot remove a
   pre-filter flag.

The union is what V3 acts on. Each comp's flags and reasons go into the verification record and
the offer's ``comps_rejected`` list (A7-18).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.base import BaseAgent
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import a7 as prompts
from wholesaling.db.models.enums import AgentId
from wholesaling.integrations.data.property import SoldComp

FLAG_NON_ARMS_LENGTH = "non_arms_length"
FLAG_NEW_BUILD = "new_build"
FLAG_RENOVATED_FLIP = "renovated_flip"
FLAG_LOT_VALUE_OUTLIER = "lot_value_outlier"
COMP_FLAGS: tuple[str, ...] = (
    FLAG_NON_ARMS_LENGTH,
    FLAG_NEW_BUILD,
    FLAG_RENOVATED_FLIP,
    FLAG_LOT_VALUE_OUTLIER,
)

OUTLIER_MULTIPLE = Decimal("2.5")
NAL_PRICE_RATIO = Decimal("0.60")
FLIP_WINDOW_DAYS = 365
FLIP_PRIOR_RATIO = Decimal("0.70")

_NAL_WORDS = re.compile(
    r"\b(family|relative|estate transfer|quit\s*claim|quitclaim|intra-?family|related part(y|ies)"
    r"|gift deed|no mls exposure|off-?market transfer)\b",
    re.I,
)
_FLIP_WORDS = re.compile(
    r"\b(fully renovated|completely remodeled|renovated|remodeled|flip)\b", re.I
)
_LOT_WORDS = re.compile(
    r"\b(teardown|tear-down|value in land|land value|lot value|builder lot)\b", re.I
)
_NEW_WORDS = re.compile(r"\b(new construction|new build|newly built|builder warranty)\b", re.I)

# Per-agent override: A7's comp audit always routes to the Sonnet comp-audit model (ENG-Q8).
MODEL_OVERRIDES: Mapping[str, Mapping[str, str]] = {"A7": {"comp_audit": "claude-sonnet-5-5"}}

AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comp_id": {"type": "string"},
                    "flags": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(COMP_FLAGS)},
                    },
                    "reasons": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["comp_id", "flags", "reasons"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["verdicts", "rationale"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class CompVerdict:
    comp_id: str
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # "prefilter" | "model"

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    def add(self, flag: str, reason: str, source: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        if reason not in self.reasons:
            self.reasons.append(reason)
        if source not in self.sources:
            self.sources.append(source)

    def to_json(self) -> dict[str, Any]:
        return {
            "comp_id": self.comp_id,
            "flags": list(self.flags),
            "reasons": list(self.reasons),
            "sources": list(self.sources),
        }


@dataclass(slots=True)
class CompAuditResult:
    verdicts: dict[str, CompVerdict]
    run_id: uuid.UUID | None
    model: str | None
    rationale: str

    @property
    def flagged_ids(self) -> list[str]:
        return [cid for cid, v in self.verdicts.items() if v.flagged]

    def to_json(self) -> dict[str, Any]:
        return {
            "verdicts": [v.to_json() for v in self.verdicts.values()],
            "flagged_ids": self.flagged_ids,
            "run_id": str(self.run_id) if self.run_id else None,
            "model": self.model,
            "rationale": self.rationale,
        }


def _surname(name: str | None) -> str | None:
    if not name:
        return None
    parts = [p for p in re.split(r"[\s,]+", name.strip()) if p]
    if not parts:
        return None
    # "Last, First" or "First Last"; entity names (LLC, Trust) never match.
    if re.search(r"\b(llc|inc|trust|lp|ltd|corp|co)\b", name, re.I):
        return None
    return (parts[0] if "," in name else parts[-1]).lower()


def prefilter_flags(comps: Sequence[SoldComp]) -> dict[str, CompVerdict]:
    """Deterministic flags from the records alone (see module docstring)."""
    verdicts = {c.comp_id: CompVerdict(comp_id=c.comp_id) for c in comps}
    if not comps:
        return verdicts
    med = Decimal(str(median([c.price_per_sqft for c in comps])))
    for c in comps:
        v = verdicts[c.comp_id]
        ppsf = c.price_per_sqft
        remarks = c.remarks or ""
        # new build
        if c.year_built is not None and c.year_built >= c.sale_date.year - 1:
            v.add(
                FLAG_NEW_BUILD,
                f"year built {c.year_built} vs sale year {c.sale_date.year}",
                "prefilter",
            )
        elif _NEW_WORDS.search(remarks):
            v.add(FLAG_NEW_BUILD, f"remarks: {remarks!r}", "prefilter")
        # lot-value outlier
        if med > 0 and ppsf > med * OUTLIER_MULTIPLE:
            v.add(
                FLAG_LOT_VALUE_OUTLIER,
                f"price/sqft {ppsf} > {OUTLIER_MULTIPLE}x median {med}",
                "prefilter",
            )
        elif _LOT_WORDS.search(remarks):
            v.add(FLAG_LOT_VALUE_OUTLIER, f"remarks: {remarks!r}", "prefilter")
        # renovated flip
        if c.prior_sale_date is not None:
            held = (c.sale_date - c.prior_sale_date).days
            recent = 0 <= held <= FLIP_WINDOW_DAYS
            cheap_prior = (
                c.prior_sale_price is not None
                and c.sale_price > 0
                and c.prior_sale_price <= c.sale_price * FLIP_PRIOR_RATIO
            )
            if recent and (cheap_prior or _FLIP_WORDS.search(remarks)):
                v.add(
                    FLAG_RENOVATED_FLIP,
                    f"resold {held} days after purchase at {c.prior_sale_price}",
                    "prefilter",
                )
        # non-arm's-length
        s, b = _surname(c.seller_name), _surname(c.buyer_name)
        if s is not None and b is not None and s == b:
            v.add(FLAG_NON_ARMS_LENGTH, f"buyer and seller share surname {s!r}", "prefilter")
        elif _NAL_WORDS.search(remarks):
            v.add(FLAG_NON_ARMS_LENGTH, f"remarks: {remarks!r}", "prefilter")
        elif med > 0 and ppsf < med * NAL_PRICE_RATIO:
            v.add(
                FLAG_NON_ARMS_LENGTH,
                f"price/sqft {ppsf} < {NAL_PRICE_RATIO}x median {med} (below-market transfer)",
                "prefilter",
            )
    return verdicts


def merge_model_verdicts(
    base: dict[str, CompVerdict], parsed: Mapping[str, Any]
) -> dict[str, CompVerdict]:
    """Add the model's flags to the pre-filter verdicts (unknown comp ids / flags are dropped)."""
    for item in parsed.get("verdicts") or []:
        if not isinstance(item, Mapping):
            continue
        cid = str(item.get("comp_id", ""))
        if cid not in base:
            continue
        flags = [str(f) for f in (item.get("flags") or []) if str(f) in COMP_FLAGS]
        reasons = [str(r) for r in (item.get("reasons") or [])]
        for i, flag in enumerate(flags):
            reason = reasons[i] if i < len(reasons) else f"model flagged {flag}"
            base[cid].add(flag, reason, "model")
    return base


class A7CompAuditAgent(BaseAgent):
    """Sonnet comp audit. ``audit`` returns pre-filter + model verdicts for every comp."""

    agent_id = AgentId.A7
    PROMPT_VERSION = prompts.PROMPT_VERSION
    SYSTEM_PROMPT = prompts.SYSTEM_PROMPT
    DEFAULT_MAX_TOKENS = 1500

    DEFAULT_PROMPT_VALUES: Mapping[str, Any] = {"company": "our team"}

    def __init__(
        self,
        runner: AgentRunner,
        session: AsyncSession,
        *,
        correlation_id: str | None = None,
        prompt_values: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            runner,
            session,
            correlation_id=correlation_id,
            prompt_values={**self.DEFAULT_PROMPT_VALUES, **(prompt_values or {})},
        )

    def audit_prompt(self, subject: Mapping[str, Any], comps: Sequence[SoldComp]) -> str:
        return (
            f"{prompts.AUDIT_INSTRUCTIONS}\n\n"
            f"SUBJECT:\n{self.as_json(dict(subject))}\n\n"
            f"COMPS:\n{self.as_json([c.to_json() for c in comps])}"
        )

    async def audit(
        self, subject: Mapping[str, Any], comps: Sequence[SoldComp], *, as_of: date | None = None
    ) -> CompAuditResult:
        verdicts = prefilter_flags(comps)
        if not comps:
            return CompAuditResult(verdicts=verdicts, run_id=None, model=None, rationale="no comps")
        request = self.build_request(
            TaskKind.COMP_AUDIT,
            self.audit_prompt({**subject, "as_of": as_of.isoformat() if as_of else None}, comps),
            json_schema=AUDIT_SCHEMA,
        )
        response, run_id = await self.run(TaskKind.COMP_AUDIT, request)
        if response.parsed is None:
            msg = "A7 comp audit returned no structured output"
            raise StructuredOutputError(msg)
        merged = merge_model_verdicts(verdicts, response.parsed)
        return CompAuditResult(
            verdicts=merged,
            run_id=run_id,
            model=response.model,
            rationale=str(response.parsed.get("rationale", "")),
        )


__all__ = [
    "AUDIT_SCHEMA",
    "COMP_FLAGS",
    "FLAG_LOT_VALUE_OUTLIER",
    "FLAG_NEW_BUILD",
    "FLAG_NON_ARMS_LENGTH",
    "FLAG_RENOVATED_FLIP",
    "MODEL_OVERRIDES",
    "A7CompAuditAgent",
    "CompAuditResult",
    "CompVerdict",
    "merge_model_verdicts",
    "prefilter_flags",
]
