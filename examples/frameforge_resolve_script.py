"""FrameForge — run this from INSIDE DaVinci Resolve.

Use this when external scripting is unavailable (free version has no
"External scripting using" preference). It runs in Resolve's own interpreter,
where the API is fully available.

INSTALL
-------
Copy this file to Resolve's script folder so it shows up in the menu:

    %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\

  (full path, usually:
   C:\\Users\\snake\\AppData\\Roaming\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\FrameForge.py)

Then in Resolve:  Workspace -> Scripts -> FrameForge

Or paste it into  Workspace -> Console  (set the console to Py3 first).

WHAT IT DOES
------------
Runs a bounded adaptive-cache loop: for RUN_SECONDS it polls the playhead a few
times a second, recomputes priorities, and nudges Smart Cache toward the
highest-priority timeline segment. Resolve's UI will be sluggish while it runs
(single-threaded script) — that's expected for this prototype.

Turn on  Playback -> Render Cache -> Smart  first, and watch the colour bar
under the timeline ruler.
"""

import os
import sys
import time

# --- make the FrameForge package importable from inside Resolve --------------
REPO = r"C:\Users\snake\OneDrive\Documents\GitHub\RenderFlow"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# --- config -----------------------------------------------------------------
RUN_SECONDS = 60          # how long the loop runs before returning control
POLL_INTERVAL = 0.4       # seconds between playhead polls
SEGMENTS_PER_POLL = 1     # how many segments to (re)cache per poll
DRY_RUN = False           # True = only print the schedule, never move playhead

# --- get the Resolve handle ------------------------------------------------
try:
    resolve  # injected as a global when launched from the Scripts menu / Console
except NameError:
    import DaVinciResolveScript as dvr_script
    resolve = dvr_script.scriptapp("Resolve")

from frameforge import CostEstimator, Scheduler
from resolve.adapter import ResolveAdapter


def main():
    adapter = ResolveAdapter(resolve=resolve)

    timeline = adapter.read_timeline()
    est = CostEstimator()
    sched = Scheduler(cost_estimator=est)
    sched.load_timeline(timeline)

    print("FrameForge :: in-app adaptive cache")
    print("timeline %r  fps=%s  clips=%d" % (timeline.name, timeline.fps, len(timeline)))
    for c in timeline:
        print("  %-20s [%6d-%-6d] cost~%5.1f  %s"
              % (c.name, c.start, c.end, est.estimate(c), list(c.effects) or "?"))
    print("-" * 56)

    deadline = time.time() + RUN_SECONDS
    dispatched = 0
    while time.time() < deadline:
        playhead = adapter.read_playhead()
        sched.update_playhead(playhead)

        order = sched.priority_order()
        if not order:
            print("all segments scheduled; stopping.")
            break

        for job in order[:SEGMENTS_PER_POLL]:
            print("playhead=%8.1f dir=%+d  ->  cache %-20s (priority %.3f)"
                  % (playhead, sched.direction, job.name, job.priority))
            if not DRY_RUN:
                adapter.request_cache(job.start, job.end)
            sched.mark_cached(job.id)
            dispatched += 1

        time.sleep(POLL_INTERVAL)

    print("-" * 56)
    print("done. %d segments dispatched to cache." % dispatched)


main()
