"""Priorities react to playhead motion, playback direction and revisit history.

Runs three scenarios against the "hard" timeline and prints how the cache
priority order changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import Scheduler
from frameforge.simulation import FakeEditor, hard_timeline


def show(sched: Scheduler, label: str) -> None:
    order = sched.priority_order()
    top = "  ".join(f"{j.name}({j.priority:.2f})" for j in order[:4])
    print(f"  {label:<34} playhead={sched.playhead:7.0f} dir={sched.direction:+d}  ->  {top}")


def main() -> None:
    tl = hard_timeline()
    editor = FakeEditor(tl, seed=1)

    sched = Scheduler()
    sched.load_timeline(tl)

    print("timeline:", [f"{c.name}[{c.start}-{c.end}] {list(c.effects)}" for c in tl], "\n")

    print("Scenario 1: playing forward from frame 300")
    for pos in list(editor.linear_playback(300, step=30))[:8]:
        sched.update_playhead(pos)
    show(sched, "after forward playback")

    print("\nScenario 2: editor scrubs backward to the intro")
    for pos in [780, 640, 500, 360, 220, 120]:
        sched.update_playhead(pos)
    show(sched, "after backward scrub")

    print("\nScenario 3: ping-pong between 'denoise' and 'stack'")
    for pos in editor.ping_pong(900, 1740, jumps=12, dwell=6):
        sched.update_playhead(pos)
    show(sched, "after repeated revisits")
    hot = sorted(sched.pending_jobs(), key=lambda j: sched._visit_score.get(j.id, 0), reverse=True)[:3]
    print("   revisit scores:", {j.name: round(sched._visit_score.get(j.id, 0), 2) for j in hot})


if __name__ == "__main__":
    main()
