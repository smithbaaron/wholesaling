"""``wholesaling-pilot``: run the synthetic investor pilot and write its report.

    wholesaling-pilot run [--market Houston --market Columbus] [--level L0|L1|L2]
                          [--temporal] [--out report.md] [--json report.json]
                          [--db sqlite+aiosqlite:///pilot.db] [--no-approve] [--strict]
                          [--seed N] [--quiet]

Exit status: 0 when every scenario passes (scenarios that fail only on a documented known
defect count as passing unless ``--strict``), 1 when any scenario fails or an invariant breaks,
2 on a usage error or when the Temporal test server is unavailable for ``--temporal``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from wholesaling.pilot.report import PilotReport
from wholesaling.pilot.scenarios import PILOT_MARKETS
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

INVESTOR_LEVEL_ACTIONS = (
    PolicyAction.INVESTOR_INITIAL_OUTREACH,
    PolicyAction.INVESTOR_FOLLOWUP,
    PolicyAction.INVESTOR_REPLY,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wholesaling-pilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the synthetic pilot")
    run.add_argument(
        "--market",
        action="append",
        dest="markets",
        metavar="NAME",
        help=f"pilot market (repeatable; default {', '.join(PILOT_MARKETS)})",
    )
    run.add_argument(
        "--level",
        choices=[lvl.value for lvl in AutonomyLevel],
        default=AutonomyLevel.L0.value,
        help="autonomy level for the three investor actions (default L0)",
    )
    run.add_argument(
        "--temporal",
        action="store_true",
        help="drive real workflows through the Temporal time-skipping test server",
    )
    run.add_argument("--out", type=Path, help="write the Markdown report here")
    run.add_argument("--json", type=Path, dest="json_out", help="write the JSON report here")
    run.add_argument("--db", help="database URL (default TEST_DATABASE_URL or SQLite in memory)")
    run.add_argument(
        "--no-approve",
        action="store_true",
        help="operator approves nothing (queued drafts stay queued)",
    )
    run.add_argument("--strict", action="store_true", help="known defects fail the run too")
    run.add_argument("--seed", type=int, default=20261027)
    run.add_argument("--quiet", action="store_true", help="print only the verdict line")
    return parser


def level_overrides(level: str) -> dict[PolicyAction, AutonomyLevel]:
    lvl = AutonomyLevel(level)
    if lvl is AutonomyLevel.L0:
        return {}
    return dict.fromkeys(INVESTOR_LEVEL_ACTIONS, lvl)


async def _run(args: argparse.Namespace) -> PilotReport:
    markets = tuple(args.markets) if args.markets else PILOT_MARKETS
    overrides = level_overrides(args.level)
    if args.temporal:
        from wholesaling.pilot.temporal_driver import TemporalPilotRunner

        temporal_runner = TemporalPilotRunner(markets=markets, database_url=args.db)
        return await temporal_runner.run(
            policy_level_overrides=overrides,
            approve_everything=not args.no_approve,
            seed=args.seed,
        )
    from wholesaling.pilot.runner import PilotRunner

    runner = PilotRunner(markets=markets, database_url=args.db)
    return await runner.run(
        policy_level_overrides=overrides,
        approve_everything=not args.no_approve,
        seed=args.seed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(_run(args))
    except RuntimeError as exc:
        if "Temporal" in str(exc):
            print(f"error: {exc}", file=sys.stderr)  # noqa: T201 - CLI output
            return 2
        raise
    if args.out is not None:
        args.out.write_text(report.render_markdown(), encoding="utf-8")
    if args.json_out is not None:
        args.json_out.write_text(report.to_json(), encoding="utf-8")
    if args.quiet:
        verdict = "PASS" if report.ok else "FAIL"
        print(  # noqa: T201 - CLI output
            f"{verdict}: {len(report.passed_scenarios)} passed, "
            f"{len(report.known_defect_scenarios)} known-defect, {len(report.failed)} failed"
        )
    else:
        print(report.render_text())  # noqa: T201 - CLI output
    ok = report.ok_strict() if args.strict else report.ok
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["INVESTOR_LEVEL_ACTIONS", "build_parser", "level_overrides", "main"]
