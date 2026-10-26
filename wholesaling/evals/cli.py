"""``wholesaling-evals``: run the golden sets and gate on the committed baseline.

    wholesaling-evals run [--agent A4 [--agent A5 ...]] [--provider mock|anthropic]
                          [--out report.md] [--json report.json] [--baseline path]
                          [--update-baseline] [--no-gate] [--tag TAG] [--golden-dir DIR]
                          [--verbose]
    wholesaling-evals list [--agent A4] [--golden-dir DIR]

Exit status: 0 when no case regresses against the baseline (``--no-gate``: when the run itself
completed), 1 on a regression, 2 on a usage error or when ``--provider anthropic`` is requested
without ``ANTHROPIC_API_KEY``.

``--provider mock`` (default) scripts the model from each case (``model`` block or synthesised
from ``expected``), which checks the deterministic processing around the model.
``--provider anthropic`` sends the same cases to the real models; the baseline is scripted-mode
data, so the gate is informational there unless ``--baseline`` points at a live baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from wholesaling.config.settings import get_settings
from wholesaling.evals.cases import AGENT_TASKS, GoldenSchemaError, load_all_cases
from wholesaling.evals.report import (
    BASELINE_PATH,
    EvalReport,
    build_report,
    compare_to_baseline,
    load_baseline,
    write_baseline,
)
from wholesaling.evals.runners import EvalUnavailable, ProviderMode, ScriptSource, run_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wholesaling-evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the golden sets")
    run.add_argument(
        "--agent",
        action="append",
        dest="agents",
        choices=sorted(AGENT_TASKS),
        help="restrict to one agent (repeatable); default every agent",
    )
    run.add_argument(
        "--provider",
        choices=["mock", "anthropic"],
        default="mock",
        help="mock = scripted from the case (default); anthropic = live models",
    )
    run.add_argument(
        "--script-from",
        choices=["case", "expected"],
        default="case",
        dest="script_source",
        help=(
            "mock mode: script the model from the case's model block when it has one (case, "
            "default) or always synthesise it from expected (expected)"
        ),
    )
    run.add_argument("--out", type=Path, help="write the Markdown report here")
    run.add_argument("--json", type=Path, dest="json_out", help="write the JSON report here")
    run.add_argument(
        "--baseline", type=Path, default=BASELINE_PATH, help="baseline to gate against"
    )
    run.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline from this run (scripted mode only) instead of gating",
    )
    run.add_argument("--no-gate", action="store_true", help="report only; never exit 1")
    run.add_argument("--tag", action="append", dest="tags", help="only cases with this tag")
    run.add_argument("--golden-dir", type=Path, help="golden set directory (tests/agents/golden)")
    run.add_argument("--verbose", action="store_true", help="print passing cases too")

    ls = sub.add_parser("list", help="list the golden cases")
    ls.add_argument("--agent", action="append", dest="agents", choices=sorted(AGENT_TASKS))
    ls.add_argument("--golden-dir", type=Path)
    return parser


def _write_outputs(report: EvalReport, args: argparse.Namespace) -> None:
    if args.out:
        args.out.write_text(report.to_markdown(include_passes=args.verbose))
    if args.json_out:
        args.json_out.write_text(json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n")


async def _run(args: argparse.Namespace) -> EvalReport:
    cases = load_all_cases(args.golden_dir, agents=args.agents, tags=args.tags)
    settings = get_settings()
    mode: ProviderMode = args.provider
    source: ScriptSource = args.script_source
    results = await run_all(cases, settings=settings, mode=mode, script_source=source)
    return build_report(results, mode=mode)


def _cmd_list(args: argparse.Namespace) -> int:
    cases = load_all_cases(args.golden_dir, agents=args.agents)
    for c in cases:
        tags = f" [{', '.join(c.tags)}]" if c.tags else ""
        print(f"{c.agent}/{c.task}\t{c.id}\t({c.source}){tags}")  # noqa: T201
    print(f"{len(cases)} case(s)")  # noqa: T201
    return 0


def _quiet_logging() -> None:
    """Keep the run log (one structlog line per model call) out of the report output."""
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.verbose:
        _quiet_logging()
    try:
        report = asyncio.run(_run(args))
    except EvalUnavailable as exc:
        print(f"skipped: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    _write_outputs(report, args)
    for agent, s in sorted(report.agents.items()):
        print(  # noqa: T201
            f"{agent}: {s.passed}/{s.total} pass ({s.pass_rate:.1%}), {s.model_calls} model calls"
        )
    for r in report.results:
        if not r.passed or args.verbose:
            print(r.diff())  # noqa: T201
    if args.update_baseline:
        if report.mode != "mock":
            print("refusing to write a live-mode baseline", file=sys.stderr)  # noqa: T201
            return 2
        target = write_baseline(report, args.baseline)
        print(f"baseline written to {target}")  # noqa: T201
        return 0
    if args.no_gate:
        return 0
    try:
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError) as exc:
        print(f"no usable baseline at {args.baseline}: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    gate = compare_to_baseline(report, baseline, agents=args.agents)
    print(gate.summary())  # noqa: T201
    for cid in gate.new_failures:
        print(f"  new failure: {cid}")  # noqa: T201
    return 0 if gate.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _cmd_list(args)
        return _cmd_run(args)
    except GoldenSchemaError as exc:
        print(f"golden schema error: {exc}", file=sys.stderr)  # noqa: T201
        return 2


def entrypoint() -> None:  # pragma: no cover - console script shim
    sys.exit(main())


__all__ = ["build_parser", "entrypoint", "main"]
