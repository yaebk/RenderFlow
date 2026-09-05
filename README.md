# RenderFlow — FrameForge

**Adaptive render-cache scheduler for video editing.**

Expensive viewport effects (motion blur, noise reduction, optical flow, Fusion…)
make interactive playback stutter because frames can't render in real time.
Instead of caching a timeline front-to-back, **FrameForge decides what to cache
_next_ based on what the editor is doing right now** — playhead position,
playback direction, and which sections the editor keeps returning to.

```
FrameForge (adaptive scheduler)   <-- the actual project
        |
   +----+----------------+
   v                     v
ResolveAdapter      FrameForgeRenderer (optional, later)
   v                     v
DaVinci Resolve     custom benchmark engine
```

The core scheduler is **independent of DaVinci Resolve** so it can be tested on a
simulated timeline first, then driven by a real Resolve session.

## Layout

| Path | What it is |
|---|---|
| [frameforge/timeline.py](frameforge/timeline.py) | Neutral `Clip` / `Timeline` data model |
| [frameforge/cost.py](frameforge/cost.py) | `CostEstimator` — static effect costs, learns from measured render times |
| [frameforge/scheduler.py](frameforge/scheduler.py) | `Scheduler` — priority heap, `P(c) = w₁C + w₂D + w₃V + w₄H` |
| [frameforge/cache.py](frameforge/cache.py) | `CacheState` — frame budget + value-based eviction |
| [frameforge/metrics.py](frameforge/metrics.py) | Playback metrics (dropped frames, FPS, 1% low, hit rate…) |
| [frameforge/simulation.py](frameforge/simulation.py) | Fake timelines + editor traces (linear / scrub / ping-pong) |
| [resolve/connect.py](resolve/connect.py) | Locates & imports `DaVinciResolveScript` |
| [resolve/adapter.py](resolve/adapter.py) | `ResolveAdapter` — read timeline/clips/playhead, drive Smart Cache |
| [examples/](examples/) | Runnable demos (see below) |
| [tests/](tests/) | `pytest` suite for the scheduler and cache |

## Scheduler priority

For each not-yet-cached segment `c`:

```
P(c) = w_cost · C   normalised render cost      (expensive work is worth caching)
     + w_prox · D   proximity to the playhead   (near work is needed sooner)
     + w_dir  · V   playback-direction match    (cache ahead of the play cursor)
     + w_hist · H   revisit score               (sections the editor keeps hitting)
```

Every signal moves as the editor works, so the heap is rebuilt lazily on each
`update_playhead()`. Weights live in `SchedulerConfig`.

Public API (matches the handoff):

```python
sched.update_playhead(position)
sched.calculate_priority(job)      # also: priority_order(), next_job()
sched.record_render_time(job, seconds)
```

## Run it

```bash
python examples/phase1_demo.py        # FLAG 1 — handoff example: cache order B, C, A
python examples/adaptive_demo.py      # FLAG 8/9 — order reacts to direction + revisits
python examples/benchmark.py          # FLAG 10/11 — none vs sequential vs FrameForge
python examples/resolve_driver.py --simulate   # FLAG 7 — full loop, offline dry-run

# With DaVinci Resolve running (project + timeline open, external scripting = Local):
python examples/resolve_probe.py      # FLAG 2/3/4 — what does the API actually expose?
python examples/resolve_driver.py     # FLAG 7 — live: read playhead, drive the cache
```

**Free version of Resolve** (no "External scripting using" preference → external
Python can't attach): use [frameforge_standalone.py](frameforge_standalone.py), a
single dependency-free file. Copy it to
`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\` and
run it from **Workspace → Scripts → frameforge_standalone**. Config is the block
at the top of the file.

Sample benchmark output (non-linear editing trace, tight render + cache budget):

```
strategy        avg_fps   low_1pct_fps   dropped   hit_rate
none               2.9          1.2         1776      0.00
sequential         5.3          1.2          926      0.48
frameforge        15.4          2.4          448      0.75
```

## Tests

```bash
pip install pytest
python -m pytest -q
```

## Resolve integration notes (FLAG 4)

The biggest risk is Resolve API limitations, not the algorithm. What the
scripting API gives us today:

* ✅ connect, get project / timeline, enumerate video tracks & clips, read the
  playhead (`GetCurrentTimecode`), move the playhead (`SetCurrentTimecode`).
* ⚠️ **Effect enumeration** is partial — Fusion comps are detectable
  (`GetFusionCompCount`); ResolveFX/OpenFX are not, so `adapter.effect_detector`
  falls back to clip-name / marker conventions. Improving this is Person 2's job.
* ❌ **No documented per-clip "render now" call and no cache-state readback.**
  FrameForge therefore drives Smart Cache indirectly: it parks the playhead /
  render range over the highest-priority segment (`ResolveAdapter.request_cache`).

Run `examples/resolve_probe.py` against your Resolve build to confirm.

## Development flags

- [x] FLAG 0 — repository
- [x] FLAG 1 — scheduler on simulated timeline
- [x] FLAG 6 — detect/rank expensive sections (static cost model + learning hook)
- [x] FLAG 7 — scheduler ↔ timeline driver loop (`resolve_driver.py`)
- [x] FLAG 8 — reprioritise on playhead move
- [x] FLAG 9 — playback direction + revisit history
- [x] FLAG 10 — performance metrics
- [~] FLAG 11 — benchmark harness (simulated; run against Resolve Smart Cache next)
- [ ] FLAG 2–5 — live Resolve connection / cache control (needs Resolve; probe ready)
- [ ] FLAG 12–15 — dashboard, custom renderer, final write-up
