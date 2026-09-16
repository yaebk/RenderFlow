"""Smoke-test the bridge from a terminal.

    python -m renderflow.bridge             # connect and describe what is open
    python -m renderflow.bridge --shutdown  # stop the bridge inside Resolve
"""

import argparse
import sys

from renderflow.bridge.client import Bridge, BridgeUnavailable, RemoteError, RemoteObject, connect


def describe(resolve) -> int:
    print("resolve  :", resolve.GetVersionString(), "on page", resolve.GetCurrentPage())
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        print("project  : none open")
        return 0
    print("project  :", project.GetName())
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        print("timeline : none open")
        return 0
    tracks = int(timeline.GetTrackCount("video"))
    clips = sum(len(timeline.GetItemListInTrack("video", i) or []) for i in range(1, tracks + 1))
    print(f"timeline : {timeline.GetName()}  ({clips} clips on {tracks} video tracks, "
          f"playhead {timeline.GetCurrentTimecode()})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m renderflow.bridge", description=__doc__)
    parser.add_argument("--shutdown", action="store_true", help="stop the running bridge")
    parser.add_argument("--prefer", choices=["auto", "direct", "bridge"], default="auto")
    args = parser.parse_args(argv)

    try:
        if args.shutdown:
            Bridge.discover().connect().shutdown()
            print("bridge stopped")
            return 0
        resolve = connect(prefer=args.prefer)
        # Blackmagic's own proxy answers None for any attribute, so hasattr() would lie here.
        if isinstance(resolve, RemoteObject):
            info = resolve._bridge.ping()
            print(f"bridge   : {resolve._bridge.host}:{resolve._bridge.port}  "
                  f"(up {info['uptime']:.0f}s, {info['requests']} requests served)")
        else:
            print("bridge   : not needed - connected directly (Studio)")
        return describe(resolve)
    except BridgeUnavailable as exc:
        print("cannot connect:", exc, file=sys.stderr)
        return 2
    except RemoteError as exc:
        print("Resolve raised:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
