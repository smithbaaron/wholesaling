"""Investor outreach copy (REQUIREMENTS.md section 8).

- ``INITIAL_BODY`` is CPY-01 **verbatim**, parameterised only by ``{first_name}`` and ``{market}``
  (CPY-02: "Houston" becomes the target market) plus ``{sender_name}`` for "[Your Name]". It is
  approved; changing a character of it is a spec change, not a code change.
- ``INITIAL_SUBJECTS`` are the relevance-based subject variants for INV-11 experiments on the
  initial message only. No urgency, no deception.
- ``FOLLOWUP_BODIES`` are three short drafts for the same thread (CPY-03). They are *not*
  approved copy: each is ``pending_approval`` until PRE-16 clears it. The third carries the
  CPY-04 tone verbatim.
- ``footer`` builds the CAN-SPAM block (CMP-01): sender identity, physical address, opt-out.

Nothing here calls a model.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from wholesaling.config.settings import Settings

ApprovalStatus = Literal["approved", "pending_approval"]
APPROVED: Final[ApprovalStatus] = "approved"
PENDING_APPROVAL: Final[ApprovalStatus] = "pending_approval"


class TemplateRenderError(ValueError):
    """Unknown or missing placeholder."""


class ProhibitedClaimError(ValueError):
    """XC-07: text would assert inventory, a property under contract, or exclusivity."""


@dataclass(frozen=True, slots=True)
class Template:
    key: str
    body: str
    approval_status: ApprovalStatus
    source_row: str  # REQUIREMENTS.md row this text comes from

    @property
    def placeholders(self) -> frozenset[str]:
        return placeholders(self.body)


# --------------------------------------------------------------------------------------------
# CPY-01: approved verbatim. Line structure: greeting / paragraph / question paragraph /
# "Best," / sender.
# --------------------------------------------------------------------------------------------

INITIAL_BODY: Final[str] = (
    "Hi {first_name},\n"
    "\n"
    "We're always working on new wholesale real estate opportunities throughout the {market} "
    "area and are looking to connect with investors interested in off-market deals.\n"
    "\n"
    "Are you currently purchasing properties in {market}? If so, would you mind sharing your "
    "current buy box so we can send opportunities that match your investment criteria?\n"
    "\n"
    "Best,\n"
    "{sender_name}"
)

INITIAL_TEMPLATE: Final[Template] = Template(
    key="investor_initial", body=INITIAL_BODY, approval_status=APPROVED, source_row="CPY-01"
)

# INV-11: relevance-based variants for the initial only. Deterministically assigned per
# investor by A3; never presented to the operator as a list to pick from.
INITIAL_SUBJECTS: Final[tuple[str, ...]] = (
    "Off-market deals in {market}",
    "Buying in {market}? Quick question about your buy box",
    "{market} wholesale opportunities - what are you looking for?",
)

# --------------------------------------------------------------------------------------------
# CPY-03 / CPY-04: three follow-ups in the same thread. Drafts pending PRE-16 approval.
# --------------------------------------------------------------------------------------------

FOLLOWUP_BODIES: Final[tuple[Template, ...]] = (
    Template(
        key="investor_followup_1",
        body=(
            "Hi {first_name},\n"
            "\n"
            "Following up on my note from last week. We're still connecting with investors who "
            "buy in {market} and would like to understand what you look for. Are you actively "
            "purchasing right now?\n"
            "\n"
            "Best,\n"
            "{sender_name}"
        ),
        approval_status=PENDING_APPROVAL,
        source_row="CPY-03",
    ),
    Template(
        key="investor_followup_2",
        body=(
            "Hi {first_name},\n"
            "\n"
            "Checking in once more. If you are buying in {market}, a quick reply with your buy "
            "box (property type, areas, price range, condition) would let us send only the "
            "opportunities that fit.\n"
            "\n"
            "Best,\n"
            "{sender_name}"
        ),
        approval_status=PENDING_APPROVAL,
        source_row="CPY-03",
    ),
    Template(
        key="investor_followup_3",
        body=(
            "Hi {first_name},\n"
            "\n"
            "Did we miss the mark, or is now simply not a good time? Let us know when would be "
            "better to reconnect.\n"
            "\n"
            "Best,\n"
            "{sender_name}"
        ),
        approval_status=PENDING_APPROVAL,
        source_row="CPY-04",
    ),
)

# --------------------------------------------------------------------------------------------
# XC-07 guard
# --------------------------------------------------------------------------------------------

# Phrases that assert inventory, contracts or exclusivity. Matched case-insensitively on
# whitespace-normalised text, in every parameter value and in the rendered result.
PROHIBITED_PHRASES: Final[tuple[str, ...]] = (
    "under contract",
    "we have",
    "we've got",
    "exclusive",
    "inventory",
    "our properties",
    "our listings",
    "properties available",
    "deals available",
    "guaranteed",
    "act now",
    "limited time",
)

_WS_RE = re.compile(r"\s+")


def find_prohibited(text: str) -> str | None:
    """Return the first prohibited phrase found in ``text``, or ``None``."""
    norm = _WS_RE.sub(" ", text).lower()
    for phrase in PROHIBITED_PHRASES:
        if phrase in norm:
            return phrase
    return None


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------

_FORMATTER = string.Formatter()


def placeholders(text: str) -> frozenset[str]:
    """Placeholder names in a ``str.format`` template (``{name}`` only; no attributes/indexes)."""
    names: set[str] = set()
    for _literal, field_name, _spec, _conv in _FORMATTER.parse(text):
        if field_name is None:
            continue
        if not field_name or not field_name.isidentifier():
            msg = f"unsupported placeholder {field_name!r}; use plain {{name}} placeholders"
            raise TemplateRenderError(msg)
        names.add(field_name)
    return frozenset(names)


def render(template: Template | str, **params: str) -> str:
    """Fill ``template`` with ``params``.

    Raises ``TemplateRenderError`` for a parameter the template does not use or a placeholder
    with no value, and ``ProhibitedClaimError`` when a value or the result carries an XC-07
    phrase. Values are substituted literally (no nested formatting).
    """
    body = template.body if isinstance(template, Template) else template
    needed = placeholders(body)
    given = frozenset(params)
    unknown = given - needed
    if unknown:
        msg = f"unknown placeholder(s) {sorted(unknown)}; template uses {sorted(needed)}"
        raise TemplateRenderError(msg)
    missing = needed - given
    if missing:
        msg = f"missing value(s) for {sorted(missing)}"
        raise TemplateRenderError(msg)
    for name, value in params.items():
        if not isinstance(value, str):
            msg = f"placeholder {name!r} must be a string, got {type(value).__name__}"
            raise TemplateRenderError(msg)
        if not value.strip():
            msg = f"placeholder {name!r} is blank"
            raise TemplateRenderError(msg)
        hit = find_prohibited(value)
        if hit is not None:
            msg = f"value for {name!r} contains prohibited phrase {hit!r} (XC-07)"
            raise ProhibitedClaimError(msg)
    out = body
    # Literal substitution so a value containing braces cannot inject a placeholder.
    for name, value in params.items():
        out = out.replace("{" + name + "}", value)
    hit = find_prohibited(out)
    if hit is not None:
        msg = f"rendered text contains prohibited phrase {hit!r} (XC-07)"
        raise ProhibitedClaimError(msg)
    return out


# --------------------------------------------------------------------------------------------
# CAN-SPAM footer (CMP-01)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    name: str
    company: str
    postal_address: str | None
    opt_out_text: str

    @classmethod
    def from_settings(cls, settings: Settings) -> SenderIdentity:
        return cls(
            name=settings.sender_name,
            company=settings.sender_company,
            postal_address=settings.sender_postal_address,
            opt_out_text=settings.opt_out_text,
        )


def footer(sender_identity: SenderIdentity) -> str:
    """Identity, physical address and opt-out line. Refuses to render without the last two."""
    if not sender_identity.name.strip() or not sender_identity.company.strip():
        msg = "sender name and company are required (CMP-01)"
        raise ValueError(msg)
    address = (sender_identity.postal_address or "").strip()
    if not address:
        msg = "SENDER_POSTAL_ADDRESS is not set; a physical address is required (CMP-01, PRE-02)"
        raise ValueError(msg)
    opt_out = sender_identity.opt_out_text.strip()
    lowered = opt_out.lower()
    if not opt_out or ("unsubscribe" not in lowered and "opt out" not in lowered):
        msg = "OPT_OUT_TEXT must tell the recipient how to opt out (CMP-01)"
        raise ValueError(msg)
    identity = f"{sender_identity.name.strip()}, {sender_identity.company.strip()}"
    return f"--\n{identity}\n{address}\n{opt_out}"


__all__ = [
    "APPROVED",
    "FOLLOWUP_BODIES",
    "INITIAL_BODY",
    "INITIAL_SUBJECTS",
    "INITIAL_TEMPLATE",
    "PENDING_APPROVAL",
    "PROHIBITED_PHRASES",
    "ApprovalStatus",
    "ProhibitedClaimError",
    "SenderIdentity",
    "Template",
    "TemplateRenderError",
    "find_prohibited",
    "footer",
    "placeholders",
    "render",
]
