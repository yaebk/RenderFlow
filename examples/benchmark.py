"""Benchmark the adaptive scheduler against naive caching strategies.

A simulated playback benchmark - no real pixels, no host required:

* Each displayed frame is "fast" if its segment is fully cached, else "slow"
  (slower the more expensive the segment's effects are).
* Between displayed frames the renderer gets a fixed budget of frames. Segments
  render incrementally and only count as cached once fully rendered.
* The cache has a frame budget; when it is full the strategy must evict, and an
  evicted segment has to be rendered again before it can be served.

Four arms, so the result says *which* idea earns the win rather than bundling
scheduling and eviction together:

    arm             what to render next        what to evict
    -------------   ------------------------   ---------------------------
    none            nothing                    -
    sequential      sweep left to right        oldest (LRU)
    adaptive+lru    FrameForge priority        oldest (LRU)
    frameforge      FrameForge priority        cheapest to rebuild

    sequential -> adaptive+lru   isolates the scheduling contribution
    adaptive+lru -> frameforge   isolates the eviction contribution

READ THIS BEFORE QUOTING THE NUMBERS
------------------------------------
Frame cost here is a *formula*, not a measurement, so this compares the model
against itself. It shows the scheduling policy behaves as designed; it does not
show real-world speedup. For that, run the FFmpeg host against a real file.

Fairness rules this benchmark has to obey, each of which was violated by an
earlier version:

1. Both strategies get the same render budget for the whole trace. Sequential
   must wrap around and keep re-rendering evicted segments rather than stopping
   after one pass.
2. Re-renders after eviction are counted identically for both strategies.
3. A render counts as wasted if that cached copy is evicted before it ever
   serves a displayed frame - same rule for both.
4. The scheduler sees exactly one playhead update per displayed frame, so its
   history decay runs at the same rate as the trace.
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
CACHE_BUDGET_FRAMES = 900       # ~a third of the timeline -> eviction matters


def long_timeline(fps: float = 24.0) -> Timeline:
    pattern = [
        ("plain", []),
        ("mblur", ["Motion Blur"]),
        ("denoise", ["Noise Reduction"]),
        ("plain", []),
        ("composite", ["Composite"]),
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


ARMS = {
    #  name           picker         evictor
    "none":         ("none",       "lru"),
    "sequential":   ("sequential", "lru"),
    "adaptive+lru": ("adaptive",   "lru"),
    "frameforge":   ("adaptive",   "value"),
}


def run(arm: str, seed: int = 7) -> dict:
    picker, evictor = ARMS[arm]
    tl = long_timeline()
    est = CostEstimator()
    costs = {c.name: est.estimate(c) for c in tl}
    lengths = {c.name: c.length for c in tl}
    trace = list(FakeEditor(tl, seed=seed).mixed_session())

    metrics = Metrics(target_fps=tl.fps)
    cache = CacheState(budget_frames=CACHE_BUDGET_FRAMES)
    progress = {c.name: 0 for c in tl}
    # Did the current cached copy of this segment ever serve a displayed frame?
    served: dict[str, bool] = {}

    sched = Scheduler(cost_estimator=est)
    sched.load_timeline(tl)
    job_id = {job.name: job.id for job in sched.pending_jobs()}

    order = [c.name for c in tl]
    seq_idx = 0

    def keep_expensive(entry):          # frameforge: cost + recency + revisits
        revisits = sched._visit_score.get(entry.job_id, 0.0)
        return entry.est_cost * 0.5 + entry.last_used * 0.01 + revisits

    def keep_recent(entry):             # sequential: plain LRU
        return entry.last_used

    def pick_sequential():
        """Sweep left to right, wrapping - never stalls after one pass."""
        nonlocal seq_idx
        for k in range(len(order)):
            idx = (seq_idx + k) % len(order)
            if progress[order[idx]] < lengths[order[idx]]:
                seq_idx = idx
                return order[idx]
        return None

    def pick_frameforge():
        job = sched.next_job()
        return job.name if job else None

    for pos in trace:
        # ---- display one frame -------------------------------------------
        clip = tl.clip_at(pos)
        name = clip.name if clip else None
        hit = bool(name and name in cache and progress[name] >= lengths[name])
        if name:
            cache.touch(name)
            if hit:
                served[name] = True
        metrics.record_frame(
            FAST_FRAME_MS if hit else frame_cost_ms(costs.get(name, 0.0)), cache_hit=hit
        )

        if picker == "none":
            continue

        # Exactly one playhead update per displayed frame (fairness rule 4).
        if picker == "adaptive":
            sched.update_playhead(pos)

        # ---- the renderer's budget between displayed frames ---------------
        budget = RENDER_BUDGET_PER_TICK
        while budget > 0:
            target = pick_frameforge() if picker == "adaptive" else pick_sequential()
            if target is None:
                break

            progress[target] += 1
            budget -= 1
            metrics.record_render(used=True)   # corrected below if it's wasted

            if progress[target] < lengths[target]:
                continue

            # Segment finished: admit it, and handle whatever got pushed out.
            served[target] = False
            if picker == "adaptive":
                sched.mark_cached(job_id[target])
            score = keep_expensive if evictor == "value" else keep_recent
            for evicted in cache.admit(target, lengths[target], costs[target], score):
                progress[evicted] = 0
                # Fairness rule 2: counted the same way for both strategies.
                metrics.rerenders_after_scrub += 1
                # Fairness rule 3: work thrown away before it was ever used.
                if not served.get(evicted, False):
                    metrics.frames_rendered_unused += lengths[evicted]
                if picker == "adaptive":
                    sched.invalidate(job_id[evicted])

    summary = metrics.summary()
    summary["frames_rendered"] = metrics.frames_rendered
    return summary


def main() -> None:
    seeds = [int(a) for a in sys.argv[1:]] or list(range(1, 13))
    cols = ["avg_fps", "drop_rate", "cache_hit_rate",
            "wasted_render_rate", "rerenders_after_scrub"]

    per_arm = {arm: [] for arm in ARMS}
    for seed in seeds:
        results = {arm: run(arm, seed) for arm in ARMS}
        # Fairness rule 1, enforced rather than assumed: every arm that renders
        # at all must get the same total render budget on the same trace.
        budgets = {r["frames_rendered"] for a, r in results.items() if a != "none"}
        assert len(budgets) == 1, f"unequal render budgets on seed {seed}: {budgets}"
        for arm, result in results.items():
            per_arm[arm].append(result)

    print(f"mean of {len(seeds)} randomised editor traces "
          f"(seeds {seeds[0]}-{seeds[-1]})")
    print("equal render budget per arm, verified per seed")
    print()
    print(f"{'arm':<14} " + "  ".join(f"{c:>20}" for c in cols))
    for arm, runs in per_arm.items():
        row = {c: sum(r[c] for r in runs) / len(runs) for c in cols}
        print(f"{arm:<14} " + "  ".join(f"{row[c]:>20.3f}" for c in cols))

    print()
    print("Simulated, not measured: frame cost is a formula, so this shows the")
    print("scheduling policy behaves as designed - not a real-world speedup.")
    print()
    print("sequential   -> adaptive+lru  isolates the scheduling contribution")
    print("adaptive+lru -> frameforge    isolates the eviction contribution")


if __name__ == "__main__":
    main()
