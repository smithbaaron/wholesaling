"""Group deed records into investor candidates (REQUIREMENTS.md INV-06).

Merge rule (conservative, by design; BG Step 5 "merge conservatively, keep source evidence"):

1. Records whose buyer names share one **exact normalised entity key**
   (``normalize_entity_name``) are the same candidate. This already folds ``Acme Holdings,
   L.L.C.`` / ``ACME HOLDINGS LLC`` / ``Acme Holdings Limited Liability Company``.
2. Two candidates with *different* keys merge only when **both** hold:
   a. their buyer mailing addresses normalise to the same key (``normalize_address``), and
   b. their names are fuzzy-similar: Jaccard similarity of the name tokens with entity suffixes
      removed is at least ``NAME_SIMILARITY_MIN`` (0.6), e.g. ``GULF COAST RENTALS`` vs
      ``GULF COAST RENTALS II`` (0.75 merges); ``FAST CLOSE HOUSTON`` vs ``HEIGHTS HOLDINGS``
      at the same registered-agent suite (0.0) does not.
   A shared address alone never merges (registered agents and PO boxes are shared by
   strangers); a similar name alone never merges (``Gulf Coast Rentals LLC`` at two different
   addresses stays two candidates). ``Acme Trust`` and ``Acme LLC`` at one mailing address do
   merge: same principal, two vehicles.
3. Individuals (no entity suffix) never merge across keys: two people with the same mailing
   address are a household, not an entity.

Every record stays attached to its candidate as evidence; nothing is dropped here. Resales
(the investor as *seller*) are attached to the candidate whose keys match the seller name so
A1 can derive hold periods.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from wholesaling.db.normalize import normalize_address, normalize_entity_name
from wholesaling.db.repositories.investors import MailingAddress
from wholesaling.integrations.data.models import DeedRecord

NAME_SIMILARITY_MIN = 0.6
_SUFFIX_TOKENS = frozenset({"LLC", "INC", "CORP", "LP", "LLP", "LTD", "TRUST", "CO"})


@dataclass(slots=True)
class InvestorCandidate:
    """One resolved buyer: all name spellings, all purchases, all resales."""

    primary_key: str
    entity_keys: set[str]
    names: list[str]  # raw spellings in first-seen order
    purchases: list[DeedRecord]
    resales: list[DeedRecord] = field(default_factory=list)
    mailing_address_keys: set[str] = field(default_factory=set)

    @property
    def display_name(self) -> str:
        return self.names[0]

    @property
    def is_entity(self) -> bool:
        return any(k.split()[-1] in _SUFFIX_TOKENS for k in self.entity_keys if k)

    def mailing_address(self) -> MailingAddress | None:
        for rec in self.purchases:
            if rec.buyer_mailing_address:
                return MailingAddress(
                    line1=rec.buyer_mailing_address,
                    city=rec.buyer_mailing_city,
                    state=rec.buyer_mailing_state,
                    postal_code=rec.buyer_mailing_zip,
                )
        return None


def _address_key(rec: DeedRecord) -> str | None:
    if not rec.buyer_mailing_address:
        return None
    return normalize_address(
        rec.buyer_mailing_address,
        rec.buyer_mailing_city,
        rec.buyer_mailing_state,
        rec.buyer_mailing_zip,
    )


def _name_tokens(entity_key: str) -> set[str]:
    return {t for t in entity_key.split() if t not in _SUFFIX_TOKENS}


def name_similarity(key_a: str, key_b: str) -> float:
    """Jaccard similarity of suffix-stripped name tokens, 0..1."""
    a, b = _name_tokens(key_a), _name_tokens(key_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _merge(into: InvestorCandidate, other: InvestorCandidate) -> None:
    into.entity_keys |= other.entity_keys
    for n in other.names:
        if n not in into.names:
            into.names.append(n)
    into.purchases.extend(other.purchases)
    into.resales.extend(other.resales)
    into.mailing_address_keys |= other.mailing_address_keys


def resolve_entities(records: Iterable[DeedRecord]) -> list[InvestorCandidate]:
    """Group ``records`` into candidates per the module rule. Order of the result follows the
    first appearance of each candidate's earliest-seen key; purchases keep input order."""
    by_key: dict[str, InvestorCandidate] = {}
    order: list[str] = []
    all_records = list(records)

    # Pass 1: exact normalised key.
    for rec in all_records:
        key = normalize_entity_name(rec.buyer_name)
        if not key:
            continue
        cand = by_key.get(key)
        if cand is None:
            cand = InvestorCandidate(primary_key=key, entity_keys={key}, names=[], purchases=[])
            by_key[key] = cand
            order.append(key)
        display = " ".join(rec.buyer_name.split())
        if display not in cand.names:
            cand.names.append(display)
        cand.purchases.append(rec)
        addr = _address_key(rec)
        if addr:
            cand.mailing_address_keys.add(addr)

    # Pass 2: shared mailing address + fuzzy name, entities only.
    by_address: dict[str, list[str]] = {}
    for key in order:
        for addr in by_key[key].mailing_address_keys:
            by_address.setdefault(addr, []).append(key)
    merged_into: dict[str, str] = {}

    def root(k: str) -> str:
        while k in merged_into:
            k = merged_into[k]
        return k

    for keys in by_address.values():
        for i, ka in enumerate(keys):
            for kb in keys[i + 1 :]:
                ra, rb = root(ka), root(kb)
                if ra == rb:
                    continue
                a, b = by_key[ra], by_key[rb]
                if not (a.is_entity and b.is_entity):
                    continue
                if name_similarity(ra, rb) >= NAME_SIMILARITY_MIN:
                    _merge(a, b)
                    merged_into[rb] = ra

    candidates = [by_key[k] for k in order if k not in merged_into]

    # Pass 3: attach resales (investor as seller) to the matching candidate.
    key_to_cand: dict[str, InvestorCandidate] = {}
    for cand in candidates:
        for k in cand.entity_keys:
            key_to_cand[k] = cand
    for rec in all_records:
        seller_key = normalize_entity_name(rec.seller_name)
        cand = key_to_cand.get(seller_key)
        if cand is not None:
            cand.resales.append(rec)
    return candidates


__all__ = ["NAME_SIMILARITY_MIN", "InvestorCandidate", "name_similarity", "resolve_entities"]
