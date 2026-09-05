"""FLAG 2 / FLAG 3 / FLAG 4 - prove what the Resolve Python API exposes.

Run this with DaVinci Resolve open (a project + timeline loaded) and external
scripting enabled.  It reports each Phase 2 milestone as pass/fail so you can
judge the API-limitation risk before building further.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve.adapter import ResolveAdapter, ResolveUnavailable


def main() -> int:
    print("FrameForge :: Resolve API probe\n" + "-" * 40)
    try:
        adapter = ResolveAdapter.connect()
    except ResolveUnavailable as exc:
        print(f"[FAIL] connect to Resolve\n       {exc}")
        return 1
    print("[ ok ] milestone 1: connected to Resolve scripting")

    try:
        proj = adapter.project
        print(f"[ ok ] milestone 2: current project = {proj.GetName()!r}")
    except ResolveUnavailable as exc:
        print(f"[FAIL] milestone 2: {exc}")
        return 1

    try:
        timeline = adapter.read_timeline()
    except ResolveUnavailable as exc:
        print(f"[FAIL] milestone 3-6: {exc}")
        return 1
    print(
        f"[ ok ] milestone 3-6: timeline {timeline.name!r} "
        f"fps={timeline.fps} clips={len(timeline)}"
    )
    tracks = sorted({c.track for c in timeline})
    print(f"        video tracks: {tracks}")
    for clip in list(timeline)[:12]:
        print(
            f"        - T{clip.track} {clip.name:<20} "
            f"[{clip.start}-{clip.end}] effects={list(clip.effects) or '?'}"
        )
    with_effects = sum(1 for c in timeline if c.effects)
    print(f"        effect info recovered for {with_effects}/{len(timeline)} clips")

    try:
        ph = adapter.read_playhead()
        print(f"[ ok ] milestone 7: playhead frame = {ph:.0f}")
    except ResolveUnavailable as exc:
        print(f"[FAIL] milestone 7: {exc}")

    support = adapter.probe_cache_controls()
    print("[info] milestone 8: cache-related settings readable:")
    for key, ok in support.items():
        print(f"        {'yes' if ok else ' no'}  {key}")

    moved = adapter.set_playhead(ph + 1 if 'ph' in dir() else 100)
    print(f"[{'ok ' if moved else 'FAIL'}] milestone 9: can move playhead programmatically = {moved}")
    print(
        "\nNote: Resolve exposes no documented per-clip 'render now' or cache-state\n"
        "readback API (FLAG 4). FrameForge drives Smart Cache by parking the\n"
        "playhead/render-range over the highest-priority segment."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
