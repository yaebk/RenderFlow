"""RenderFlow command line.

    python -m renderflow report             # everything: scan + decode + render cost + plan
    python -m renderflow report --json      # the same, for tools and agents
    python -m renderflow fix                # show the planned fixes (nothing changes)
    python -m renderflow fix --apply        # make them, journaled
    python -m renderflow fix --undo         # reverse everything from the journal

    python -m renderflow scan               # inventory + rule-of-thumb findings only
    python -m renderflow profile            # scan + decode speed of every clip
    python -m renderflow render-cost        # time a sample of every timeline clip in Resolve's queue

    python -m renderflow install-bridge     # put the in-app launcher in Resolve's Scripts menu
"""

import argparse
import json
import sys
from dataclasses import asdict

from renderflow.bridge.client import BridgeUnavailable, RemoteError, connect
from renderflow.bridge.install import install as install_bridge
from renderflow.fix import Journal, apply, plan_text, undo
from renderflow.profile import (
    DEFAULT_SAMPLE_S,
    DEFAULT_SEEKS,
    FFmpegMissing,
    MeasurementCache,
    measured_text,
    profile,
)
from renderflow.rendercost import (
    DEFAULT_BUDGET_S,
    DEFAULT_SECONDS,
    DEFAULT_SHORT_SECONDS,
    RenderCache,
    render_cost,
    render_findings,
)
from renderflow.report import full_report
from renderflow.scan import findings_text, scan


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m renderflow", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefer", choices=["auto", "direct", "bridge"], default="auto",
                        help="how to reach Resolve (default: direct on Studio, else the bridge)")
    sub = parser.add_subparsers(dest="command", required=True)

    def render_args(p):
        p.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                       help="long render sample in timeline seconds (default %(default)s)")
        p.add_argument("--short-seconds", type=float, default=DEFAULT_SHORT_SECONDS,
                       help="short sample that cancels per-job set-up, 0 to skip (default %(default)s)")
        p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_S,
                       help="max wall seconds per long sample (default %(default)s)")
        p.add_argument("--remeasure", action="store_true",
                       help="render every sample again instead of reusing earlier results")

    p_rep = sub.add_parser("report", help="scan, measure decode and render cost, plan fixes")
    p_rep.add_argument("--json", action="store_true", help="machine-readable output")
    p_rep.add_argument("--no-decode", action="store_true", help="skip FFmpeg decode measurement")
    p_rep.add_argument("--no-render", action="store_true", help="skip render-queue measurement")
    p_rep.add_argument("--proxies", choices=["auto", "all", "none"], default="auto",
                       help="plan proxies for measured-slow clips (auto), every long-GOP clip (all), or none")
    render_args(p_rep)

    p_fix = sub.add_parser("fix", help="plan, apply or undo fixes")
    group = p_fix.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="make the planned changes")
    group.add_argument("--undo", action="store_true", help="reverse every journaled change")
    p_fix.add_argument("--proxies", choices=["auto", "all", "none"], default="auto")
    p_fix.add_argument("--no-markers", action="store_true")
    p_fix.add_argument("--no-settings", action="store_true")
    p_fix.add_argument("--no-render", action="store_true",
                       help="plan without render-cost measurement (faster; no Smart-cache decision)")
    render_args(p_fix)

    p_scan = sub.add_parser("scan", help="inventory the open project and report likely bottlenecks")
    p_scan.add_argument("--json", action="store_true", help="machine-readable output")

    p_prof = sub.add_parser("profile", help="scan, then measure how fast each clip decodes here")
    p_prof.add_argument("--json", action="store_true", help="machine-readable output")
    p_prof.add_argument("--sample", type=float, default=DEFAULT_SAMPLE_S,
                        help="length of the decode sample per clip (default %(default)s)")
    p_prof.add_argument("--seeks", type=int, default=DEFAULT_SEEKS,
                        help="random-access seeks to time per clip, 0 to skip (default %(default)s)")
    p_prof.add_argument("--hwaccel", default=None,
                        help="also try a hardware decoder, e.g. d3d11va or cuda (what Studio would use)")
    p_prof.add_argument("--remeasure", action="store_true",
                        help="measure every clip again instead of reusing earlier results")

    p_rc = sub.add_parser("render-cost", help="render a sample of every timeline clip and time it")
    p_rc.add_argument("--json", action="store_true", help="machine-readable output")
    render_args(p_rc)

    p_inst = sub.add_parser("install-bridge",
                            help="copy the in-app launcher into Resolve's Scripts > Utility folder")
    p_inst.add_argument("--dest", default=None, help="scripts folder to write to (default: Resolve's)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install-bridge":
        try:
            target = install_bridge(args.dest)
        except (OSError, RuntimeError) as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"installed {target}")
        print("In Resolve: Workspace > Scripts > RenderFlow_Bridge. Then check with:  "
              "python -m renderflow.bridge")
        return 0
    render_kw = {}
    if hasattr(args, "seconds"):
        render_kw = {"seconds": args.seconds, "short_seconds": args.short_seconds,
                     "budget_s": args.budget}
        if args.remeasure:
            render_kw["cache"] = RenderCache(None)
    try:
        resolve = connect(prefer=args.prefer, timeout=1200)

        if args.command == "report":
            report = full_report(resolve, decode=not args.no_decode, render=not args.no_render,
                                 proxies=args.proxies, progress=_stderr, **render_kw)
            if args.json:
                json.dump(report.to_dict(), sys.stdout, indent=2)
                print()
            else:
                print(report.text())
            return 0

        if args.command == "fix":
            if args.undo:
                journal = Journal()
                print(f"undoing {len(journal)} change(s) ..." if journal.entries
                      else "the journal is empty - checking the timeline for leftover markers ...")
                problems = undo(resolve, journal, progress=print)
                print("done." if not problems else f"{len(problems)} problem(s): " + "; ".join(problems))
                return 1 if problems else 0
            report = full_report(resolve, decode=True, render=not args.no_render,
                                 proxies=args.proxies, progress=_stderr, **render_kw)
            actions = [a for a in report.actions
                       if not (args.no_markers and a.kind == "marker")
                       and not (args.no_settings and a.kind in ("setting", "clip-setting"))]
            print(plan_text(actions, report.notes))
            if not actions:
                return 0
            if not args.apply:
                print("\nnothing changed. Re-run with --apply to make these changes, "
                      "--undo later to reverse them.")
                return 0
            print("\napplying ...")
            problems = apply(resolve, actions, progress=print)
            print("done. Undo any time with:  python -m renderflow fix --undo"
                  if not problems else f"{len(problems)} problem(s): " + "; ".join(problems))
            return 1 if problems else 0

        if args.command == "render-cost":
            rc = render_cost(resolve, progress=_stderr, **render_kw)
            findings = render_findings(rc)
            if args.json:
                data = rc.to_dict()
                data["findings"] = [asdict(f) for f in findings]
                json.dump(data, sys.stdout, indent=2)
                print()
            else:
                print(rc.text())
                print()
                empty = ("no findings - every measured clip renders at or above real time."
                         if any(s.ok for s in rc.samples)
                         else "no findings - nothing on this timeline was long enough to measure.")
                print(findings_text(findings, empty))
            return 0

        report = scan(resolve)
        results = {}
        if args.command == "profile":
            cache = MeasurementCache(None) if args.remeasure else None
            results = profile(report, sample_s=args.sample, seeks=args.seeks,
                              hwaccel=args.hwaccel, cache=cache, progress=_stderr)
        if args.json:
            json.dump(report.to_dict(), sys.stdout, indent=2)
            print()
        else:
            if args.command == "profile":
                print(measured_text(report, results))
                print()
            print(report.text())
        return 0
    except BridgeUnavailable as exc:
        print("cannot reach Resolve:", exc, file=sys.stderr)
        return 2
    except FFmpegMissing as exc:
        print(exc, file=sys.stderr)
        return 3
    except (RemoteError, RuntimeError) as exc:
        print("Resolve error:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
