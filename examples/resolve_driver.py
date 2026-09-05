"""FLAG 7 - connect the scheduler to a real Resolve timeline and drive caching.

    DaVinci Resolve
          v
    ResolveAdapter        (read timeline, clips, playhead / write cache hints)
          v
    CostEstimator + Scheduler
          v
    "cache this segment next"  --> ResolveAdapter.request_cache(...)

Usage:
    python examples/resolve_driver.py                 # live, needs Resolve open
    python examples/resolve_driver.py --simulate      # offline dry-run
    python examples/resolve_driver.py --once          # single pass, then exit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CostEstimator, Scheduler
from frameforge.simulation import FakeEditor, hard_timeline
from resolve.adapter import ResolveAdapter, ResolveUnavailable


class SimulatedAdapter:
    """Stand-in for :class:`ResolveAdapter` so the driver runs without Resolve."""

    def __init__(self) -> None:
        self._timeline = hard_timeline()
        self._trace = FakeEditor(self._timeline, seed=7).mixed_session()
        self._playhead = 0.0
        self.cached: list[tuple[str, int, int]] = []

    def read_timeline(self):
        return self._timeline

    def read_playhead(self) -> float:
        try:
            self._playhead = next(self._trace)
        except StopIteration:
            pass
        return self._playhead

    def request_cache(self, start_frame: int, end_frame: int, dwell_s: float = 0.0) -> None:
        self.cached.append(("segment", start_frame, end_frame))

    def probe_cache_controls(self):
        return {}


def build_driver(simulate: bool):
    if simulate:
        return SimulatedAdapter(), True
    try:
        return ResolveAdapter.connect(), False
    except ResolveUnavailable as exc:
        print(f"[warn] Resolve unavailable ({exc}); falling back to --simulate.\n")
        return SimulatedAdapter(), True


def main() -> int:
    ap = argparse.ArgumentParser(description="FrameForge adaptive cache driver")
    ap.add_argument("--simulate", action="store_true", help="run offline against a fake timeline")
    ap.add_argument("--once", action="store_true", help="single scheduling pass then exit")
    ap.add_argument("--interval", type=float, default=0.25, help="seconds between polls")
    ap.add_argument("--per-tick", type=int, default=1, help="segments dispatched to cache per poll")
    ap.add_argument("--ticks", type=int, default=200, help="max polls before stopping")
    args = ap.parse_args()

    adapter, simulated = build_driver(args.simulate)
    mode = "SIMULATED" if simulated else "LIVE (DaVinci Resolve)"
    print(f"FrameForge driver :: {mode}\n" + "-" * 48)

    timeline = adapter.read_timeline()
    estimator = CostEstimator()
    sched = Scheduler(cost_estimator=estimator)
    sched.load_timeline(timeline)
    print(f"loaded timeline {timeline.name!r}: {len(timeline)} clips, fps={timeline.fps}")
    for clip in timeline:
        print(f"  {clip.name:<16} [{clip.start:>5}-{clip.end:<5}] "
              f"cost~{estimator.estimate(clip):5.1f} {list(clip.effects)}")
    print()

    dispatched = 0
    ticks = 1 if args.once else args.ticks
    for tick in range(ticks):
        playhead = adapter.read_playhead()
        sched.update_playhead(playhead)

        jobs = sched.take(args.per_tick)
        for job in jobs:
            adapter.request_cache(job.start, job.end)
            dispatched += 1
            print(
                f"[t{tick:04d}] playhead={playhead:8.1f} dir={sched.direction:+d}  "
                f"cache -> {job.name:<16} (priority {job.priority:.3f}, "
                f"{len(sched.pending_jobs())} pending)"
            )

        if not sched.pending_jobs():
            print(f"\nall {dispatched} segments scheduled for cache.")
            # Nothing left to do until the timeline changes; in a real session
            # you'd keep polling for edits / invalidations here.
            break
        if not args.once:
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
