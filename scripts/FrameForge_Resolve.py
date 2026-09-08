"""FrameForge for DaVinci Resolve - run this from inside Resolve.

============================================================================
INSTALL
============================================================================
1. Edit REPO below to point at your RenderFlow checkout.
2. Copy this file to Resolve's script menu folder:

     %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\

   i.e.  C:\\Users\\<you>\\AppData\\Roaming\\Blackmagic Design\\DaVinci Resolve
         \\Support\\Fusion\\Scripts\\Utility\\FrameForge_Resolve.py

3. In Resolve: open a project and a timeline, then
     Workspace -> Scripts -> FrameForge_Resolve
   Output appears in Workspace -> Console (set the dropdown to Py3).

The free edition of Resolve has no "External scripting using" preference, so
running it from the Scripts menu like this is the only route. It works on both
editions.

============================================================================
WHAT IT DOES
============================================================================
Resolve's API has no render-cache control of any kind - the word "cache"
appears once in Blackmagic's entire scripting reference, as an archiving flag.
So FrameForge does not drive Resolve's cache. It builds one:

  * picks the most valuable segment (expensive, near your playhead)
  * renders just that frame range through the render queue
  * imports the result and drops it on a "FrameForge Cache" video track

A baked clip on the top track plays with no effect processing, so the segment
becomes free. The win is the *order*: your working area goes smooth first,
rather than after a front-to-back sweep finishes.

============================================================================
BEFORE YOU RUN IT
============================================================================
This modifies your timeline. Work on a duplicate.
Start with MODE = "plan", which renders nothing and just prints the order.
MODE = "clear" removes the cache track and undoes everything.

None of this has been run against a live Resolve yet - every API call is
verified present in the bundled scripting docs, but the first real run is an
experiment. Read the console output before trusting it.
"""

import sys

# ==========================================================================
# CONFIG - edit these
# ==========================================================================
REPO = r"C:\Users\snake\OneDrive\Documents\GitHub\RenderFlow"
CACHE_DIR = r"C:\Users\snake\Documents\FrameForgeCache"

MODE = "plan"              # "plan" | "cache" | "clear"
MAX_SEGMENTS = 8           # how many segments to bake in one run
MIN_SEGMENT_FRAMES = 24    # merge composite segments shorter than this
RENDER_FORMAT = "mp4"
RENDER_CODEC = "H264"

# ==========================================================================
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from frameforge import CacheEngine, EngineConfig, describe_host          # noqa: E402
from frameforge.adapters.resolve import ResolveHost, get_resolve         # noqa: E402


def main():
    try:
        handle = get_resolve(globals().get("resolve"))
    except Exception as exc:                                   # noqa: BLE001
        print("Could not reach Resolve:", exc)
        return

    host = ResolveHost(
        handle,
        cache_dir=CACHE_DIR,
        render_format=RENDER_FORMAT,
        render_codec=RENDER_CODEC,
        dry_run=(MODE == "plan"),
    )

    print("FrameForge for DaVinci Resolve  ::  MODE =", MODE)
    print("-" * 68)

    if MODE == "clear":
        removed = host.clear_cache_track()
        print(f"removed {removed} baked clip(s) and the cache track.")
        return

    timeline = host.read_timeline()
    adopted = host.adopt_existing()
    print(f"timeline {timeline.name!r}  fps={timeline.fps}  "
          f"{len(timeline)} clips on {len({c.track for c in timeline})} track(s)")
    if adopted:
        print(f"{adopted} segment(s) already cached from a previous run")

    engine = CacheEngine(
        host, timeline,
        config=EngineConfig(min_segment_frames=MIN_SEGMENT_FRAMES),
    )
    print(f"-> {engine.pending} composite segments to consider")
    print()
    print(describe_host(host))
    print("-" * 68)

    if MODE == "plan":
        print(f"planned order (nothing will be rendered), playhead "
              f"{engine.scheduler.playhead:.0f}:")
        for rank in range(1, min(MAX_SEGMENTS, engine.pending) + 1):
            segment = engine.next_segment()
            if segment is None:
                break
            print(f"  {rank:2d}. {segment.name:<18} frames "
                  f"{segment.start:>7}-{segment.end:<7} cost~{segment.cost:6.1f}")
            engine.step(1)          # dry_run host records it without rendering
        print()
        print('Looks right? Set MODE = "cache" and run again.')
        return

    print(f"baking up to {MAX_SEGMENTS} segment(s). Resolve will be busy.")
    for result in engine.step(MAX_SEGMENTS):
        if result.ok:
            print(f"  {result.segment.name:<18} cost~{result.segment.cost:6.1f}  "
                  f"{result.seconds:7.1f}s")
        else:
            print(f"  {result.segment.name:<18} FAILED  {result.error}")

    print("-" * 68)
    print("stats:", engine.stats())
    print(f"{engine.pending} segment(s) still uncached - run again to continue.")
    print('To undo everything, set MODE = "clear" and run again.')


main()
