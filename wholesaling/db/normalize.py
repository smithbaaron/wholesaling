"""Pure normalisation functions used for dedupe keys (REQUIREMENTS.md DM-02, INV-06, INV-08).

Everything here is deterministic and side-effect free. Callers store the *raw* value alongside
the normalised key so nothing is lost.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "normalize_address",
    "normalize_email",
    "normalize_entity_name",
    "normalize_phone",
]

# --------------------------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------------------------

# Providers that ignore dots in the local part and treat ``+tag`` as an alias of the base box.
# Only these get dot/plus folding: other providers may treat ``a.b`` and ``ab`` as different
# people, so folding there would merge strangers.
_DOT_INSENSITIVE_DOMAINS: Final[frozenset[str]] = frozenset({"gmail.com", "googlemail.com"})
_PLUS_ALIAS_DOMAINS: Final[frozenset[str]] = _DOT_INSENSITIVE_DOMAINS | frozenset(
    {"outlook.com", "hotmail.com", "live.com", "fastmail.com", "protonmail.com", "proton.me"}
)
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str) -> str:
    """Return the dedupe key for an email address.

    Rules: strip surrounding whitespace and angle brackets; lower-case the whole address;
    ``googlemail.com`` becomes ``gmail.com``; for Gmail, dots in the local part are removed;
    for Gmail and the common providers in ``_PLUS_ALIAS_DOMAINS`` the ``+tag`` suffix is
    dropped. Raises ``ValueError`` when the input is not shaped like an address.
    """
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    # "Name <addr>" display forms
    if "<" in value and value.endswith(">"):
        value = value[value.rfind("<") + 1 : -1].strip()
    value = value.lower()
    if not _EMAIL_RE.match(value):
        msg = f"not an email address: {raw!r}"
        raise ValueError(msg)
    local, _, domain = value.rpartition("@")
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain in _PLUS_ALIAS_DOMAINS:
        local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    if not local:
        msg = f"empty local part after normalisation: {raw!r}"
        raise ValueError(msg)
    return f"{local}@{domain}"


# --------------------------------------------------------------------------------------------
# Phone
# --------------------------------------------------------------------------------------------

_NON_DIGITS: Final[re.Pattern[str]] = re.compile(r"\D+")


def normalize_phone(raw: str, default_country: str = "1") -> str:
    """Return the number in E.164 (``+1XXXXXXXXXX``).

    US/NANP is the default: a bare 10-digit number gets ``+1``; an 11-digit number starting
    with ``1`` is accepted as-is. A leading ``+`` keeps the given country code as long as the
    total is 8 to 15 digits (E.164 maximum). Extensions (``x123``, ``ext. 12``) are dropped.
    Anything else raises ``ValueError``.
    """
    value = raw.strip()
    if not value:
        msg = "empty phone number"
        raise ValueError(msg)
    value = re.split(r"(?i)\s*(?:x|ext\.?|extension)\s*\d*\s*$", value)[0]
    international = value.lstrip().startswith("+")
    digits = _NON_DIGITS.sub("", value)
    if international:
        if 8 <= len(digits) <= 15:
            return f"+{digits}"
        msg = f"not a valid E.164 number: {raw!r}"
        raise ValueError(msg)
    if default_country == "1":
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10 and digits[0] not in "01" and digits[3] not in "01":
            return f"+1{digits}"
        msg = f"not a valid US phone number: {raw!r}"
        raise ValueError(msg)
    if 8 <= len(default_country) + len(digits) <= 15:
        return f"+{default_country}{digits}"
    msg = f"not a valid phone number: {raw!r}"
    raise ValueError(msg)


# --------------------------------------------------------------------------------------------
# Entity names
# --------------------------------------------------------------------------------------------

# Multi-word phrases first (longest match wins), then single tokens. Values are canonical tokens.
_ENTITY_SUFFIX_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("LIMITED LIABILITY COMPANY", "LLC"),
    ("LIMITED LIABILITY CO", "LLC"),
    ("LIMITED LIABILITY PARTNERSHIP", "LLP"),
    ("LIMITED PARTNERSHIP", "LP"),
    ("REVOCABLE LIVING TRUST", "TRUST"),
    ("IRREVOCABLE TRUST", "TRUST"),
    ("REVOCABLE TRUST", "TRUST"),
    ("LIVING TRUST", "TRUST"),
)
_ENTITY_SUFFIX_TOKENS: Final[dict[str, str]] = {
    "LLC": "LLC",
    "L.L.C.": "LLC",
    "LLC.": "LLC",
    "PLLC": "LLC",
    "INC": "INC",
    "INC.": "INC",
    "INCORPORATED": "INC",
    "CORP": "CORP",
    "CORP.": "CORP",
    "CORPORATION": "CORP",
    "CO": "CO",
    "CO.": "CO",
    "COMPANY": "CO",
    "LP": "LP",
    "L.P.": "LP",
    "LLP": "LLP",
    "L.L.P.": "LLP",
    "LTD": "LTD",
    "LTD.": "LTD",
    "LIMITED": "LTD",
    "TRUST": "TRUST",
    "TR": "TRUST",
    "TRUSTEE": "TRUST",
    "TRUSTEES": "TRUST",
}
_ENTITY_SUFFIX_CANON: Final[frozenset[str]] = frozenset(_ENTITY_SUFFIX_TOKENS.values())
_ENTITY_PUNCT: Final[re.Pattern[str]] = re.compile(r"[^A-Z0-9 ]+")
_WS: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_entity_name(raw: str) -> str:
    """Return the entity-resolution key for an owner or company name (INV-06).

    Upper-cases, folds ``&`` to ``AND``, drops a leading ``THE``, strips punctuation and
    collapses whitespace. Entity-type suffixes in any common spelling (``LLC``, ``L.L.C.``,
    ``Limited Liability Company``; ``Inc``/``Incorporated``; ``Corp``/``Corporation``;
    ``LP``/``L.P.``/``Limited Partnership``; ``Trust``/``Living Trust``/``Tr``; ``Ltd``;
    ``Co``/``Company``) collapse to one canonical token so ``Acme Holdings, L.L.C.`` and
    ``ACME HOLDINGS LLC`` map to ``ACME HOLDINGS LLC``. Only trailing suffix tokens are
    collapsed; the suffix is kept so ``Acme Trust`` and ``Acme LLC`` remain distinct entities.
    """
    value = raw.strip().upper().replace("&", " AND ")
    # Treat dotted abbreviations as plain tokens before stripping punctuation.
    tokens = [t.strip(".,;:") for t in _WS.split(value) if t.strip(".,;:")]
    tokens = [t for t in (_ENTITY_PUNCT.sub("", t) for t in tokens) if t]
    if tokens and tokens[0] == "THE":
        tokens = tokens[1:]
    text = " ".join(tokens)
    for phrase, canon in _ENTITY_SUFFIX_PHRASES:
        if text.endswith(" " + phrase) or text == phrase:
            text = text[: -len(phrase)].rstrip() + " " + canon
            break
    tokens = text.split()
    # Collapse one or more trailing suffix tokens (e.g. "HOLDINGS CO LLC" -> "HOLDINGS CO LLC",
    # "ACME LLC LLC" -> "ACME LLC"); keep the last canonical suffix only.
    suffixes: list[str] = []
    while tokens and (tokens[-1] in _ENTITY_SUFFIX_TOKENS or tokens[-1] in _ENTITY_SUFFIX_CANON):
        tok = tokens.pop()
        suffixes.append(_ENTITY_SUFFIX_TOKENS.get(tok, tok))
    if suffixes:
        # Innermost (last stripped) suffix is the primary entity type; drop duplicates.
        canon_order: list[str] = []
        for s in reversed(suffixes):
            if s not in canon_order:
                canon_order.append(s)
        tokens.extend(canon_order)
    return " ".join(tokens)


# --------------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------------

_STREET_SUFFIXES: Final[dict[str, str]] = {
    "STREET": "ST",
    "STR": "ST",
    "AVENUE": "AVE",
    "AV": "AVE",
    "ROAD": "RD",
    "DRIVE": "DR",
    "DRV": "DR",
    "LANE": "LN",
    "BOULEVARD": "BLVD",
    "BLV": "BLVD",
    "COURT": "CT",
    "CRT": "CT",
    "PLACE": "PL",
    "CIRCLE": "CIR",
    "PARKWAY": "PKWY",
    "PKY": "PKWY",
    "HIGHWAY": "HWY",
    "TRAIL": "TRL",
    "TERRACE": "TER",
    "TERR": "TER",
    "SQUARE": "SQ",
    "LOOP": "LOOP",
    "WAY": "WAY",
    "ALLEY": "ALY",
    "CROSSING": "XING",
    "EXPRESSWAY": "EXPY",
    "FREEWAY": "FWY",
    "POINT": "PT",
    "RIDGE": "RDG",
    "CREEK": "CRK",
    "HOLLOW": "HOLW",
    "HEIGHTS": "HTS",
    "JUNCTION": "JCT",
    "MOUNT": "MT",
    "MOUNTAIN": "MTN",
}
_DIRECTIONALS: Final[dict[str, str]] = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
    "N.": "N",
    "S.": "S",
    "E.": "E",
    "W.": "W",
}
_UNIT_DESIGNATORS: Final[frozenset[str]] = frozenset(
    {
        "APARTMENT",
        "APT",
        "SUITE",
        "STE",
        "UNIT",
        "BUILDING",
        "BLDG",
        "FLOOR",
        "FL",
        "ROOM",
        "RM",
        "LOT",
        "TRAILER",
        "TRLR",
        "#",
        "NO",
        "NUMBER",
    }
)
_ADDRESS_PUNCT: Final[re.Pattern[str]] = re.compile(r"[^A-Z0-9# ]+")


def normalize_address(
    line1: str,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
) -> str:
    """Return a basic dedupe key for a street address.

    Upper-cases, strips punctuation, collapses whitespace, abbreviates common street suffixes,
    directionals; every unit designator (``Apt``, ``Suite``, ``#`` ...) becomes ``UNIT`` and
    runs of them collapse (``Apt. #4B`` becomes ``UNIT 4B``). Appends ``city``,
    ``state`` and the 5-digit ZIP when given, joined by ``|``. This is a *basic* key: it is not
    a CASS-certified standardiser and does not validate deliverability. Parcel IDs remain the
    stronger dedupe key when available.
    """
    text = line1.strip().upper()
    text = re.sub(r"#\s*", " # ", text)
    tokens = [t for t in _WS.split(_ADDRESS_PUNCT.sub(" ", text)) if t]
    out: list[str] = []
    for tok in tokens:
        if tok in _UNIT_DESIGNATORS:
            if not out or out[-1] != "UNIT":
                out.append("UNIT")
        elif tok in _STREET_SUFFIXES:
            out.append(_STREET_SUFFIXES[tok])
        elif tok in _DIRECTIONALS:
            out.append(_DIRECTIONALS[tok])
        else:
            out.append(tok)
    parts = [" ".join(out)]
    if city:
        parts.append(_WS.sub(" ", _ADDRESS_PUNCT.sub(" ", city.strip().upper())).strip())
    if state:
        parts.append(state.strip().upper()[:2])
    if postal_code:
        zip5 = _NON_DIGITS.sub("", postal_code)[:5]
        if zip5:
            parts.append(zip5)
    return "|".join(parts)
