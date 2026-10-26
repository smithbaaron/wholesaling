"""Golden-case schema, loader and the migration shims for the two older formats (BO-13 #1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.evals.conftest import case_by_id
from wholesaling.evals.cases import (
    AGENT_TASKS,
    FORMAT_INBOUND_REPLIES,
    FORMAT_SELLER_INTAKE,
    FORMAT_UNIFIED,
    GOLDEN_DIR,
    GoldenCase,
    GoldenSchemaError,
    detect_format,
    golden_files,
    load_all_cases,
    load_file,
    parse_document,
    validate_case,
)


def test_every_committed_file_loads_and_validates() -> None:
    files = golden_files()
    assert {p.name for p in files} >= {
        "inbound_replies.json",
        "seller_intake.json",
        "a5_qa.json",
        "a7_comp_audit.json",
    }
    for path in files:
        parsed = load_file(path)
        assert parsed.cases, path.name
        for case in parsed.cases:
            validate_case(case)


def test_formats_are_detected_per_file() -> None:
    by_name = {p.name: load_file(p).format for p in golden_files()}
    assert by_name["inbound_replies.json"] == FORMAT_INBOUND_REPLIES
    assert by_name["seller_intake.json"] == FORMAT_SELLER_INTAKE
    assert by_name["a5_qa.json"] == FORMAT_UNIFIED
    assert by_name["a7_comp_audit.json"] == FORMAT_UNIFIED


def test_case_counts_per_agent_meet_the_bo13_minimums(all_cases: list[GoldenCase]) -> None:
    per_agent: dict[str, int] = {}
    for c in all_cases:
        per_agent[c.agent] = per_agent.get(c.agent, 0) + 1
    assert per_agent["A5"] >= 20
    assert per_agent["A7"] >= 12
    assert per_agent["S4"] >= 16
    assert per_agent["A4"] >= 24 + 5  # classify cases plus the extraction cases
    assert set(per_agent) == set(AGENT_TASKS)


def test_a4_shim_keeps_every_expected_value_from_the_raw_file() -> None:
    raw = json.loads((GOLDEN_DIR / "inbound_replies.json").read_text())
    cases = load_file(GOLDEN_DIR / "inbound_replies.json").cases
    by_id = {c.id: c for c in cases}
    for entry in raw["cases"]:
        classify = by_id[entry["id"]]
        assert classify.agent == "A4" and classify.task == "classify"
        assert dict(classify.expected) == entry["expected"]
        assert classify.model == entry["model"]
        assert classify.input["body"] == entry["body"]
        assert classify.input["subject"] == entry["subject"]
        assert classify.input["history"] == entry.get("history", [])
        if "extraction" in entry:
            extract = by_id[f"{entry['id']}:extract"]
            assert extract.task == "extract" and "extraction" in extract.tags
            assert dict(extract.expected) == entry["extraction"]["expected"]
            assert extract.model == entry["extraction"]["model"]
            assert extract.input["current"] == entry["extraction"]["current"]
        else:
            assert f"{entry['id']}:extract" not in by_id


def test_s4_shim_keeps_every_expected_value_from_the_raw_file() -> None:
    raw = json.loads((GOLDEN_DIR / "seller_intake.json").read_text())
    cases = load_file(GOLDEN_DIR / "seller_intake.json").cases
    assert len(cases) == len(raw["cases"])
    for entry, case in zip(raw["cases"], cases, strict=True):
        assert case.id == entry["id"] and case.agent == "S4" and case.task == "intake"
        assert dict(case.expected) == entry["expected"]
        assert case.model == entry["model"]
        assert case.input["known"] == entry["known"]
        assert case.input["from"] == entry["from"]


def test_a5_set_covers_the_required_scenarios(all_cases: list[GoldenCase]) -> None:
    a5 = [c for c in all_cases if c.agent == "A5"]
    tags = {t for c in a5 for t in c.tags}
    assert {
        "prohibited_claim",
        "opt_out",
        "wrong_recipient",
        "repeated_request",
        "cadence",
        "sensitive",
        "clean",
        "rewrite",
    } <= tags
    verdicts = {c.expected["verdict"] for c in a5}
    assert verdicts == {"approve", "block", "rewrite"}
    assert sum(1 for c in a5 if c.expected["model_called"] is False) >= 12
    assert sum(1 for c in a5 if c.expected["verdict"] == "approve") >= 6


def test_a7_set_covers_every_flag_and_the_model_layer(all_cases: list[GoldenCase]) -> None:
    a7 = [c for c in all_cases if c.agent == "A7"]
    flags = {f for c in a7 for v in c.expected["flagged"].values() for f in v}
    assert flags == {"non_arms_length", "new_build", "renovated_flip", "lot_value_outlier"}
    assert any("model_flags" in c.expected for c in a7)
    assert any(not c.input["comps"] for c in a7), "the empty set case"
    assert case_by_id(a7, "fixture_dirty_four_flags").input["comps"][0]["comp_id"] == "DC1"


def test_unified_roundtrip_and_to_json(all_cases: list[GoldenCase]) -> None:
    case = case_by_id(all_cases, "clean_approve_asks_missing_field", "A5")
    doc = case.to_json()
    assert set(doc) >= {"id", "agent", "task", "input", "expected", "tags", "notes"}
    again = parse_document({"cases": [doc]}, source="x.json").cases[0]
    assert again.id == case.id and dict(again.expected) == dict(case.expected)
    assert again.source_format == FORMAT_UNIFIED


def test_filters_by_agent_and_tag() -> None:
    assert {c.agent for c in load_all_cases(agents=["a5"])} == {"A5"}
    tagged = load_all_cases(tags=["extraction"])
    assert tagged and all(c.task == "extract" for c in tagged)


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        (
            {
                "cases": [
                    {
                        "id": "x",
                        "agent": "ZZ",
                        "task": "qa",
                        "input": {"a": 1},
                        "expected": {"b": 1},
                    }
                ]
            },
            "unknown agent",
        ),
        (
            {
                "cases": [
                    {
                        "id": "x",
                        "agent": "A5",
                        "task": "classify",
                        "input": {"a": 1},
                        "expected": {"b": 1},
                    }
                ]
            },
            "not one of",
        ),
        (
            {
                "cases": [
                    {"id": "x", "agent": "A5", "task": "qa", "input": {}, "expected": {"b": 1}}
                ]
            },
            "non-empty",
        ),
        ({"cases": [{"id": "x", "agent": "A5", "task": "qa", "input": {"a": 1}}]}, "missing"),
        (
            {
                "cases": [
                    {
                        "id": "x",
                        "agent": "A5",
                        "task": "qa",
                        "input": {"a": 1},
                        "expected": {"b": 1},
                        "tags": "nope",
                    }
                ]
            },
            "tags",
        ),
        ({"nope": []}, "'cases' list"),
        (
            {
                "cases": [
                    {
                        "id": "x",
                        "agent": "A5",
                        "task": "qa",
                        "input": {"a": 1},
                        "expected": {"b": 1},
                    },
                    {
                        "id": "x",
                        "agent": "A5",
                        "task": "qa",
                        "input": {"a": 1},
                        "expected": {"b": 1},
                    },
                ]
            },
            "duplicate",
        ),
    ],
)
def test_schema_errors(doc: dict[str, Any], message: str) -> None:
    with pytest.raises(GoldenSchemaError, match=message):
        parse_document(doc, source="bad.json")


def test_legacy_shims_reject_incomplete_entries() -> None:
    with pytest.raises(GoldenSchemaError, match="inbound reply case missing"):
        parse_document(
            {"_meta": {"format": FORMAT_INBOUND_REPLIES}, "cases": [{"id": "x"}]}, source="a.json"
        )
    with pytest.raises(GoldenSchemaError, match="seller intake case missing"):
        parse_document(
            {"_meta": {"format": FORMAT_SELLER_INTAKE}, "cases": [{"id": "x"}]}, source="s.json"
        )
    with pytest.raises(GoldenSchemaError, match="no expected"):
        parse_document(
            {
                "_meta": {"format": FORMAT_INBOUND_REPLIES},
                "cases": [
                    {
                        "id": "x",
                        "subject": "s",
                        "body": "b",
                        "expected": {"classification": "x"},
                        "extraction": {"model": {}},
                    }
                ],
            },
            source="a.json",
        )


def test_detect_format_from_shape() -> None:
    assert detect_format({"cases": [{"id": "x", "agent": "A5", "task": "qa"}]}) == FORMAT_UNIFIED
    assert detect_format({"cases": [{"id": "x", "known": {}}]}) == FORMAT_SELLER_INTAKE
    assert (
        detect_format({"cases": [{"id": "x", "model": {"classify": {}}}]}) == FORMAT_SELLER_INTAKE
    )
    assert detect_format({"cases": [{"id": "x", "subject": "s"}]}) == FORMAT_INBOUND_REPLIES
    assert detect_format({"cases": []}) == FORMAT_UNIFIED


def test_invalid_json_is_a_schema_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(GoldenSchemaError, match="invalid JSON"):
        load_file(bad)
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(GoldenSchemaError, match="must be an object"):
        load_file(tmp_path / "list.json")
