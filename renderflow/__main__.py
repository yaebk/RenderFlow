"""RenderFlow command line.

    python -m renderflow scan             # inventory the open project + findings
    python -m renderflow scan --json      # same, as JSON for tools and agents
"""

import argparse
import json
import sys

from renderflow.bridge.client import BridgeUnavailable, RemoteError, connect
from renderflow.scan import scan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m renderflow", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefer", choices=["auto", "direct", "bridge"], default="auto",
                        help="how to reach Resolve (default: direct on Studio, else the bridge)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_scan = sub.add_parser("scan", help="inventory the open project and report likely bottlenecks")
    p_scan.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        resolve = connect(prefer=args.prefer, timeout=120)
        if args.command == "scan":
            report = scan(resolve)
            if args.json:
                json.dump(report.to_dict(), sys.stdout, indent=2)
                print()
            else:
                print(report.text())
            return 0
    except BridgeUnavailable as exc:
        print("cannot reach Resolve:", exc, file=sys.stderr)
        return 2
    except (RemoteError, RuntimeError) as exc:
        print("Resolve error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
