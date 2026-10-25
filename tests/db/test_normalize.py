"""Pure normalisation functions (wholesaling/db/normalize.py)."""

from __future__ import annotations

import pytest

from wholesaling.db.normalize import (
    normalize_address,
    normalize_email,
    normalize_entity_name,
    normalize_phone,
)

# --- email -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Pat@Example.com ", "pat@example.com"),
        ("Pat <Pat.Doe@Example.com>", "pat.doe@example.com"),
        ("<pat@example.com>", "pat@example.com"),
        ("John.Doe+deals@Gmail.com", "johndoe@gmail.com"),
        ("j.o.h.n@googlemail.com", "john@gmail.com"),
        ("a+tag@outlook.com", "a@outlook.com"),
        # Non-Gmail providers keep dots: a.b and ab may be different people.
        ("a.b@example.com", "a.b@example.com"),
        # Unknown providers keep +tags too.
        ("a+b@example.com", "a+b@example.com"),
    ],
)
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-an-email", "a@b", "two@@example.com", "+@gmail.com"])
def test_normalize_email_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError, match=r"email|local"):
        normalize_email(raw)


def test_normalize_email_is_idempotent() -> None:
    once = normalize_email("John.Doe+x@Gmail.com")
    assert normalize_email(once) == once


# --- phone -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(713) 555-0123", "+17135550123"),
        ("713.555.0123", "+17135550123"),
        ("1-713-555-0123", "+17135550123"),
        ("+1 713 555 0123", "+17135550123"),
        ("7135550123 x204", "+17135550123"),
        ("713-555-0123 ext. 9", "+17135550123"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "555-0123", "013-555-0123", "713-155-0123", "+1", "abc"])
def test_normalize_phone_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError, match=r"phone|E\.164"):
        normalize_phone(raw)


def test_normalize_phone_other_default_country() -> None:
    assert normalize_phone("020 7946 0958", default_country="44") == "+4402079460958"


# --- entity names ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "Acme Holdings LLC",
        "ACME HOLDINGS, L.L.C.",
        "Acme Holdings Limited Liability Company",
        "Acme Holdings Limited Liability Co.",
        "The Acme Holdings LLC.",
        "acme   holdings llc",
        "Acme Holdings PLLC",
    ],
)
def test_llc_variants_share_a_key(variant: str) -> None:
    assert normalize_entity_name(variant) == "ACME HOLDINGS LLC"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Inc.", "ACME INC"),
        ("Acme Incorporated", "ACME INC"),
        ("Acme Corp", "ACME CORP"),
        ("Acme Corporation", "ACME CORP"),
        ("Acme, L.P.", "ACME LP"),
        ("Acme Limited Partnership", "ACME LP"),
        ("Acme Ltd", "ACME LTD"),
        ("Acme Limited", "ACME LTD"),
        ("Acme Co", "ACME CO"),
        ("Acme Company", "ACME CO"),
        ("Smith Family Living Trust", "SMITH FAMILY TRUST"),
        ("Smith Family Revocable Living Trust", "SMITH FAMILY TRUST"),
        ("Smith Family Trust", "SMITH FAMILY TRUST"),
        ("John Smith Tr", "JOHN SMITH TRUST"),
        ("John Smith, Trustee", "JOHN SMITH TRUST"),
        ("Acme & Sons, Inc.", "ACME AND SONS INC"),
        ("ACME LLC LLC", "ACME LLC"),
        ("Acme Co LLC", "ACME CO LLC"),
        ("John Smith", "JOHN SMITH"),
        ("  john  smith ", "JOHN SMITH"),
        ("O'Brien Properties, LLC", "OBRIEN PROPERTIES LLC"),
    ],
)
def test_normalize_entity_name(raw: str, expected: str) -> None:
    assert normalize_entity_name(raw) == expected


def test_entity_suffix_distinguishes_entity_types() -> None:
    keys = {normalize_entity_name(n) for n in ["Acme LLC", "Acme Trust", "Acme Inc", "Acme"]}
    assert len(keys) == 4


def test_entity_name_idempotent() -> None:
    once = normalize_entity_name("The Acme Holdings, L.L.C.")
    assert normalize_entity_name(once) == once


# --- addresses -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((" 123 North Main Street ",), "123 N MAIN ST"),
        (("123 N. Main St.",), "123 N MAIN ST"),
        (
            ("123 Main Street, Apt. #4B", "Houston", "tx", "77002-1234"),
            "123 MAIN ST UNIT 4B|HOUSTON|TX|77002",
        ),
        (
            ("123 Main St Suite 200", "Houston", "TX", "77002"),
            "123 MAIN ST UNIT 200|HOUSTON|TX|77002",
        ),
        (("123 Main St #200", "Houston", "TX", "77002"), "123 MAIN ST UNIT 200|HOUSTON|TX|77002"),
        (("4500 Southwest Freeway",), "4500 SW FWY"),
        (("12 Oak Boulevard", "St. Louis", "MO", None), "12 OAK BLVD|ST LOUIS|MO"),
    ],
)
def test_normalize_address(args: tuple[str, ...], expected: str) -> None:
    assert normalize_address(*args) == expected


def test_address_idempotent() -> None:
    once = normalize_address("123 North Main Street, Apt. #4B")
    assert normalize_address(once) == once
