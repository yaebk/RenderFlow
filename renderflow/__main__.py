"""RenderFlow command line.

    python -m renderflow scan               # inventory + rule-of-thumb findings
    python -m renderflow profile            # scan, then measure decode speed of every clip
    python -m renderflow profile --json     # machine-readable, for tools and agents
    python -m renderflow render-cost        # time a sample of every timeline clip in Resolve's queue
"""

import argparse
import json
import sys

from renderflow.bridge.client import BridgeUnavailable, RemoteError, connect
from renderflow.profile import DEFAULT_SAMPLE_S, DEFAULT_SEEKS, FFmpegMissing, measured_text, profile
from renderflow.rendercost import DEFAULT_FRAMES, render_cost, render_findings
from renderflow.scan import scan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m renderflow", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefer", choices=["auto", "direct", "bridge"], default="auto",
                        help="how to reach Resolve (default: direct on Studio, else the bridge)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="inventory the open project and report likely bottlenecks")
    p_scan.add_argument("--json", action="store_true", help="machine-readable output")

    p_prof = sub.add_parser("profile", help="scan, then measure how fast each clip decodes here")
    p_prof.add_argument("--json", action="store_true", help="machine-readable output")
    p_prof.add_argument("--seconds", type=float, default=DEFAULT_SAMPLE_S,
                        help="length of the decode sample per clip (default %(default)s)")
    p_prof.add_argument("--seeks", type=int, default=DEFAULT_SEEKS,
                        help="random-access seeks to time per clip, 0 to skip (default %(default)s)")
    p_prof.add_argument("--hwaccel", default=None,
                        help="also try a hardware decoder, e.g. d3d11va or cuda (what Studio would use)")
    p_prof.add_argument("--no-cache", action="store_true", help="re-measure even if cached")

    p_rc = sub.add_parser("render-cost", help="render a short sample of every timeline clip and time it")
    p_rc.add_argument("--json", action="store_true", help="machine-readable output")
    p_rc.add_argument("--frames", type=int, default=DEFAULT_FRAMES,
                      help="frames to render per clip (default %(default)s)")
    args = parser.parse_args(argv)

    try:
        resolve = connect(prefer=args.prefer, timeout=1200)
        if args.command == "render-cost":
            rc = render_cost(resolve, frames=args.frames,
                             progress=lambda msg: print(msg, file=sys.stderr))
            findings = render_findings(rc)
            if args.json:
                data = rc.to_dict()
                data["findings"] = [f.__dict__ for f in findings]
                json.dump(data, sys.stdout, indent=2)
                print()
            else:
                print(rc.text())
                print()
                if not findings:
                    print("no findings - every clip renders at or above real time.")
                for f in findings:
                    print(f)
            return 0
        report = scan(resolve)
        results = {}
        if args.command == "profile":
            from renderflow.profile import MeasurementCache
            cache = MeasurementCache(None) if args.no_cache else None
            results = profile(report, sample_s=args.seconds, seeks=args.seeks,
                              hwaccel=args.hwaccel, cache=cache,
                              progress=lambda msg: print(msg, file=sys.stderr))
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
