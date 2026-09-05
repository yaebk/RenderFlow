"""FLAG 10 / FLAG 11 - benchmark FrameForge against naive caching strategies.

This is a *simulated* playback benchmark (no Resolve, no real pixels):

* Each displayed frame is "fast" if its segment is fully cached, else "slow"
  (slower the more expensive the segment's effects are).
* A small, fixed render budget of frames is available between each displayed
  frame.  Segments render incrementally (``progress`` frames) and only count as
  cached once fully rendered.
* The cache has a frame budget; when it's full the strategy must evict.

    strategies:
      none        - never cache
      sequential  - cache clips left to right, evict oldest
      frameforge  - adaptive scheduler + value-based eviction
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CostEstimator, Metrics, Scheduler
from frameforge.cache import CacheState
from frameforge.simulation import FakeEditor
from frameforge.timeline import Timeline

FAST_FRAME_MS = 1000.0 / 24.0
RENDER_BUDGET_PER_TICK = 6      # frames the renderer can produce between displays
CACHE_BUDGET_FRAMES = 900       # ~half the timeline fits in cache -> eviction matters


def long_timeline(fps: float = 24.0) -> Timeline:
    pattern = [
        ("plain", []),
        ("mblur", ["Motion Blur"]),
        ("denoise", ["Noise Reduction"]),
        ("plain", []),
        ("fusion", ["Fusion"]),
        ("optflow", ["Optical Flow"]),
        ("stack", ["Motion Blur", "Noise Reduction"]),
        ("plain", []),
    ]
    rows, t, seg = [], 0, 180
    for i in range(16):
        name, fx = pattern[i % len(pattern)]
        rows.append({"name": f"{name}_{i}", "start": t, "end": t + seg, "effects": fx})
        t += seg
    return Timeline.from_dicts(rows, fps=fps, name="long")


def frame_cost_ms(cost: float) -> float:
    return FAST_FRAME_MS * (1.0 + cost)


def run(strategy: str) -> dict:
    tl = long_timeline()
    est = CostEstimator()
    costs = {c.name: est.estimate(c) for c in tl}
    trace = list(FakeEditor(tl, seed=7).mixed_session())

    metrics = Metrics(target_fps=tl.fps)
    cache = CacheState(budget_frames=CACHE_BUDGET_FRAMES)
    progress: dict[str, int] = {c.name: 0 for c in tl}
    length = {c.name: c.length for c in tl}

    sched = Scheduler(cost_estimator=est)
    sched.load_timeline(tl)
    seq = [c.name for c in tl]
    seq_idx = 0

    def keep_score(entry):  # frameforge: keep expensive + recently used
        return entry.est_cost * 0.5 + entry.last_used * 0.01

    for pos in trace:
        clip = tl.clip_at(pos)
        name = clip.name if clip else None
        hit = bool(name and name in cache and progress[name] >= length[name])
        if name:
            cache.touch(name)
        metrics.record_frame(
            FAST_FRAME_MS if hit else frame_cost_ms(costs.get(name, 0.0)), cache_hit=hit
        )

        budget = RENDER_BUDGET_PER_TICK
        if strategy == "none":
            continue

        while budget > 0:
            if strategy == "frameforge":
                sched.update_playhead(pos)
                job = sched.next_job()
                if job is None:
                    break
                target = job.name
            else:  # sequential
                while seq_idx < len(seq) and progress[seq[seq_idx]] >= length[seq[seq_idx]]:
                    seq_idx += 1
                if seq_idx >= len(seq):
                    break
                target = seq[seq_idx]

            progress[target] += 1
            budget -= 1
            used = tl.clip_at(pos) and tl.clip_at(pos).name == target
            metrics.record_render(used=bool(used))

            if progress[target] >= length[target]:
                if strategy == "frameforge":
                    sched.mark_cached(next(j.id for j in sched._jobs.values() if j.name == target))
                    cache.admit(target, length[target], costs[target], keep_score)
                else:
                    cache.admit(target, length[target], costs[target],
                                lambda e: e.last_used)  # evict oldest
                # a segment evicted from cache must be re-rendered later
                for ev in list(cache.entries):
                    pass
        # reflect evictions back into progress so they get re-rendered
        for nm in list(progress):
            if progress[nm] >= length[nm] and nm not in cache:
                progress[nm] = 0
                if strategy == "frameforge":
                    sched.invalidate(next(j.id for j in sched._jobs.values() if j.name == nm))
                    metrics.rerenders_after_scrub += 1

    return metrics.summary()


def main() -> None:
    cols = ["avg_fps", "low_1pct_fps", "dropped_frames", "cache_hit_rate",
            "wasted_render_rate", "rerenders_after_scrub", "time_to_smooth_s"]
    print(f"{'strategy':<12} " + "  ".join(f"{c:>18}" for c in cols))
    for strat in ("none", "sequential", "frameforge"):
        s = run(strat)
        print(f"{strat:<12} " + "  ".join(f"{str(s[c]):>18}" for c in cols))
    print(
        "\nWith a tight render + cache budget and a scrubbing/non-linear trace,\n"
        "FrameForge should show fewer dropped frames and a higher hit rate than\n"
        "sequential caching (handoff Phase 5)."
    )


if __name__ == "__main__":
    main()
