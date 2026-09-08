"""The scheduler in its simplest form, on simulated timeline data.

The canonical example:

    clips = [A(0-300, cost 2), B(301-600, cost 9), C(601-900, cost 5)]
    playhead = 450
    => CACHE PRIORITY: 1. B  2. C  3. A
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import Scheduler, SchedulerConfig
from frameforge.simulation import sample_timeline


def main() -> None:
    timeline = sample_timeline()

    # Weight cost heavily here so the result matches the handoff's cost-driven
    # basic priority function; the adaptive demo uses the balanced defaults.
    sched = Scheduler(SchedulerConfig(w_cost=2.0, w_prox=1.0, w_dir=0.0, w_hist=0.0))
    sched.load_timeline(timeline)
    sched.update_playhead(450)

    print(f"timeline: {[c.name for c in timeline]}   playhead: {sched.playhead:.0f}\n")
    print("CACHE PRIORITY")
    for rank, job in enumerate(sched.priority_order(), start=1):
        info = sched.explain(job)
        print(
            f"  {rank}. Clip {job.name:<3} "
            f"cost={job.est_cost:<5.1f} dist={info['distance']:<6.0f} "
            f"priority={info['priority']:.3f}"
        )


if __name__ == "__main__":
    main()
