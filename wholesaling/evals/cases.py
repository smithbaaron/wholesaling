"""Golden-case schema and loader (BO-13 deliverable 1).

One schema for every agent::

    {"id": "...", "agent": "A4", "task": "classify", "input": {...}, "expected": {...},
     "tags": ["..."], "notes": "...", "model": {...optional scripted model output...}}

A file is ``{"_meta": {...}, "cases": [...]}``. Three on-disk formats are accepted:

- **unified** (``_meta.format == "unified"`` or every case carries ``agent``): the schema above,
  used by ``a5_qa.json`` and ``a7_comp_audit.json``.
- **inbound_replies** (the A4 set, BO-07): ``{id, subject, body, headers?, history, model,
  expected, extraction?}``. Migrated to one ``A4 / classify`` case per entry and, when
  ``extraction`` is present, a second ``A4 / extract`` case with the id ``<id>:extract``.
- **seller_intake** (the S4 set, BO-10): ``{id, subject, from, body, headers, history, known,
  model: {classify, extract}, expected}``. Migrated to one ``S4 / intake`` case.

The migration shims read the files as they are; nothing is rewritten on disk, so the agent
tests that drive those files directly (``tests/agents/test_a4.py`` / ``test_s4.py``) keep every
expected value. ``tags`` and ``notes`` are optional in every format (older cases have none).

Model stubbing: a case's ``model`` block, when present, is the structured output the scripted
provider returns. When it is absent the runner synthesises one from ``expected``
(``runners.model_output_from_expected``), which is the "scripted-from-expected" mode.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

GOLDEN_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "tests" / "agents" / "golden"
"""Where the committed golden sets live (``tests/agents/golden``)."""

AGENT_TASKS: Final[dict[str, tuple[str, ...]]] = {
    "A4": ("classify", "extract", "draft"),
    "A5": ("qa",),
    "S4": ("intake",),
    "A7": ("comp_audit",),
}
"""Every (agent, task) the runners know. A case outside this table fails validation."""

FORMAT_UNIFIED: Final = "unified"
FORMAT_INBOUND_REPLIES: Final = "inbound_replies"
FORMAT_SELLER_INTAKE: Final = "seller_intake"

_REQUIRED_KEYS: Final = ("id", "agent", "task", "input", "expected")


class GoldenSchemaError(ValueError):
    """A committed golden file does not match the schema."""


@dataclass(frozen=True, slots=True)
class GoldenCase:
    id: str
    agent: str
    task: str
    input: Mapping[str, Any]
    expected: Mapping[str, Any]
    tags: tuple[str, ...] = ()
    notes: str = ""
    model: Mapping[str, Any] | None = None
    source: str = ""  # file name the case came from
    source_format: str = FORMAT_UNIFIED

    def to_json(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "id": self.id,
            "agent": self.agent,
            "task": self.task,
            "input": dict(self.input),
            "expected": dict(self.expected),
            "tags": list(self.tags),
            "notes": self.notes,
        }
        if self.model is not None:
            doc["model"] = dict(self.model)
        return doc


@dataclass(slots=True)
class GoldenFile:
    path: Path
    format: str
    meta: dict[str, Any]
    cases: list[GoldenCase] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Format detection and migration shims
# --------------------------------------------------------------------------------------------


def detect_format(doc: Mapping[str, Any]) -> str:
    meta = doc.get("_meta") or {}
    declared = meta.get("format") if isinstance(meta, Mapping) else None
    if isinstance(declared, str) and declared in (
        FORMAT_UNIFIED,
        FORMAT_INBOUND_REPLIES,
        FORMAT_SELLER_INTAKE,
    ):
        return declared
    cases = doc.get("cases") or []
    if not isinstance(cases, list) or not cases:
        return FORMAT_UNIFIED
    first = cases[0]
    if not isinstance(first, Mapping):
        return FORMAT_UNIFIED
    if "agent" in first and "task" in first:
        return FORMAT_UNIFIED
    model = first.get("model")
    if "known" in first or (isinstance(model, Mapping) and "classify" in model):
        return FORMAT_SELLER_INTAKE
    return FORMAT_INBOUND_REPLIES


def _tags(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        msg = f"tags must be a list of strings, got {raw!r}"
        raise GoldenSchemaError(msg)
    return tuple(raw)


def _migrate_inbound_reply(raw: Mapping[str, Any], source: str) -> list[GoldenCase]:
    """A4 ``inbound_replies.json`` entry -> classify case (+ extract case)."""
    for key in ("id", "subject", "body", "expected"):
        if key not in raw:
            msg = f"{source}: inbound reply case missing {key!r}: {raw.get('id')!r}"
            raise GoldenSchemaError(msg)
    base_input: dict[str, Any] = {
        "subject": raw["subject"],
        "body": raw["body"],
        "headers": dict(raw.get("headers") or {}),
        "history": list(raw.get("history") or []),
    }
    tags = _tags(raw.get("tags"))
    notes = str(raw.get("notes") or "")
    out = [
        GoldenCase(
            id=str(raw["id"]),
            agent="A4",
            task="classify",
            input=base_input,
            expected=dict(raw["expected"]),
            tags=tags,
            notes=notes,
            model=dict(raw["model"]) if isinstance(raw.get("model"), Mapping) else None,
            source=source,
            source_format=FORMAT_INBOUND_REPLIES,
        )
    ]
    extraction = raw.get("extraction")
    if isinstance(extraction, Mapping):
        if "expected" not in extraction:
            msg = f"{source}: extraction block of {raw['id']!r} has no expected"
            raise GoldenSchemaError(msg)
        out.append(
            GoldenCase(
                id=f"{raw['id']}:extract",
                agent="A4",
                task="extract",
                input={**base_input, "current": dict(extraction.get("current") or {})},
                expected=dict(extraction["expected"]),
                tags=(*tags, "extraction"),
                notes=notes,
                model=(
                    dict(extraction["model"])
                    if isinstance(extraction.get("model"), Mapping)
                    else None
                ),
                source=source,
                source_format=FORMAT_INBOUND_REPLIES,
            )
        )
    return out


def _migrate_seller_intake(raw: Mapping[str, Any], source: str) -> list[GoldenCase]:
    """S4 ``seller_intake.json`` entry -> one intake case."""
    for key in ("id", "subject", "from", "body", "expected"):
        if key not in raw:
            msg = f"{source}: seller intake case missing {key!r}: {raw.get('id')!r}"
            raise GoldenSchemaError(msg)
    model = raw.get("model")
    return [
        GoldenCase(
            id=str(raw["id"]),
            agent="S4",
            task="intake",
            input={
                "subject": raw["subject"],
                "from": raw["from"],
                "body": raw["body"],
                "headers": dict(raw.get("headers") or {}),
                "history": list(raw.get("history") or []),
                "known": dict(raw.get("known") or {}),
            },
            expected=dict(raw["expected"]),
            tags=_tags(raw.get("tags")),
            notes=str(raw.get("notes") or ""),
            model=dict(model) if isinstance(model, Mapping) else None,
            source=source,
            source_format=FORMAT_SELLER_INTAKE,
        )
    ]


def _unified(raw: Mapping[str, Any], source: str) -> list[GoldenCase]:
    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        msg = f"{source}: case {raw.get('id')!r} missing {missing}"
        raise GoldenSchemaError(msg)
    model = raw.get("model")
    return [
        GoldenCase(
            id=str(raw["id"]),
            agent=str(raw["agent"]),
            task=str(raw["task"]),
            input=dict(raw["input"]),
            expected=dict(raw["expected"]),
            tags=_tags(raw.get("tags")),
            notes=str(raw.get("notes") or ""),
            model=dict(model) if isinstance(model, Mapping) else None,
            source=source,
            source_format=FORMAT_UNIFIED,
        )
    ]


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


def validate_case(case: GoldenCase) -> None:
    """Schema checks every case must pass, whatever file it came from."""
    if not case.id or not isinstance(case.id, str):
        msg = f"{case.source}: a case has no id"
        raise GoldenSchemaError(msg)
    if case.agent not in AGENT_TASKS:
        known = sorted(AGENT_TASKS)
        msg = f"{case.source}/{case.id}: unknown agent {case.agent!r} (known: {known})"
        raise GoldenSchemaError(msg)
    if case.task not in AGENT_TASKS[case.agent]:
        msg = (
            f"{case.source}/{case.id}: task {case.task!r} is not one of "
            f"{AGENT_TASKS[case.agent]} for {case.agent}"
        )
        raise GoldenSchemaError(msg)
    if not isinstance(case.input, Mapping) or not case.input:
        msg = f"{case.source}/{case.id}: input must be a non-empty object"
        raise GoldenSchemaError(msg)
    if not isinstance(case.expected, Mapping) or not case.expected:
        msg = f"{case.source}/{case.id}: expected must be a non-empty object"
        raise GoldenSchemaError(msg)
    if case.model is not None and not isinstance(case.model, Mapping):
        msg = f"{case.source}/{case.id}: model must be an object when present"
        raise GoldenSchemaError(msg)


def validate_unique_ids(cases: Iterable[GoldenCase]) -> None:
    seen: dict[str, str] = {}
    for c in cases:
        key = f"{c.agent}:{c.id}"
        if key in seen:
            msg = f"duplicate case id {c.id!r} for {c.agent} in {seen[key]} and {c.source}"
            raise GoldenSchemaError(msg)
        seen[key] = c.source


# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------


def parse_document(doc: Mapping[str, Any], *, source: str) -> GoldenFile:
    if "cases" not in doc or not isinstance(doc["cases"], list):
        msg = f"{source}: top level must have a 'cases' list"
        raise GoldenSchemaError(msg)
    fmt = detect_format(doc)
    meta = dict(doc.get("_meta") or {})
    out: list[GoldenCase] = []
    for raw in doc["cases"]:
        if not isinstance(raw, Mapping):
            msg = f"{source}: every case must be an object"
            raise GoldenSchemaError(msg)
        if fmt == FORMAT_INBOUND_REPLIES:
            out.extend(_migrate_inbound_reply(raw, source))
        elif fmt == FORMAT_SELLER_INTAKE:
            out.extend(_migrate_seller_intake(raw, source))
        else:
            out.extend(_unified(raw, source))
    for c in out:
        validate_case(c)
    validate_unique_ids(out)
    return GoldenFile(path=Path(source), format=fmt, meta=meta, cases=out)


def load_file(path: Path) -> GoldenFile:
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        msg = f"{path.name}: invalid JSON: {exc}"
        raise GoldenSchemaError(msg) from exc
    if not isinstance(doc, Mapping):
        msg = f"{path.name}: top level must be an object"
        raise GoldenSchemaError(msg)
    parsed = parse_document(doc, source=path.name)
    parsed.path = path
    return parsed


def golden_files(directory: Path | None = None) -> list[Path]:
    base = directory or GOLDEN_DIR
    return sorted(p for p in base.glob("*.json") if p.is_file())


def load_all_cases(
    directory: Path | None = None,
    *,
    agents: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
) -> list[GoldenCase]:
    """Every case in every committed golden file, validated, optionally filtered."""
    cases: list[GoldenCase] = []
    for path in golden_files(directory):
        cases.extend(load_file(path).cases)
    validate_unique_ids(cases)
    if agents:
        wanted = {a.upper() for a in agents}
        cases = [c for c in cases if c.agent in wanted]
    if tags:
        wanted_tags = set(tags)
        cases = [c for c in cases if wanted_tags & set(c.tags)]
    return cases


__all__ = [
    "AGENT_TASKS",
    "FORMAT_INBOUND_REPLIES",
    "FORMAT_SELLER_INTAKE",
    "FORMAT_UNIFIED",
    "GOLDEN_DIR",
    "GoldenCase",
    "GoldenFile",
    "GoldenSchemaError",
    "detect_format",
    "golden_files",
    "load_all_cases",
    "load_file",
    "parse_document",
    "validate_case",
    "validate_unique_ids",
]
