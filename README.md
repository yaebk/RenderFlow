# FrameForge

**An adaptive render-cache scheduler for any frame renderer.**

Rendering a timeline front-to-back wastes most of its effort on frames nobody is
about to look at. FrameForge answers a different question, continuously, as fast
as the cursor moves:

> Given where the viewer is looking *right now*, what should I render next?

It ranks not-yet-cached segments by render cost, distance from the playhead,
playback direction and revisit history — so the expensive shot you are about to
scrub into gets cached before the cheap one at the far end of the timeline.

FrameForge is **not tied to any editing application**. You give it something
that can render a frame range; it decides the order.

```python
from frameforge import CacheEngine
from frameforge.formats import load

class MyHost:
    def render(self, segment):
        my_renderer.render_range(segment.start, segment.end)

engine = CacheEngine(MyHost(), load("edit.edl"))
engine.start()                  # warms the cache on a background thread
engine.set_playhead(1200)       # call this from your playback/UI callback
```

That `render` method is the entire required contract.

---

## Why it isn't a plugin for your NLE

The obvious version of this idea is a script you paste into Premiere or Resolve
that speeds up their cache. We built that, tested it, and it does not work — not
because of our code, but because the hosts do not expose the necessary control.

A cache scheduler needs three things from a host: read the timeline, read the
playhead, and **cause a specific region to be cached**. The third is the one
that matters, and nobody offers it.

| Host | Read timeline | Live playhead | Effect / cost info | **Trigger a cache render** |
|---|---|---|---|---|
| DaVinci Resolve | yes | yes | binary only¹ | **no** |
| Premiere Pro | yes | yes² | yes | **no**² |
| After Effects | yes | yes | yes | **no** |
| Final Cut Pro | XML export only | no | no | **no** |
| Avid Media Composer | AAF export only | no | no | **no** |

¹ Measured empirically: `GetFusionCompCount()` is the only reliable effect
signal, so a 37-clip timeline priced out as 30 clips at one value and 7 at
another. One bit of information is not a cost model.
² Premiere's [Sequence API](https://ppro-scripting.docsforadobe.dev/sequence/sequence/)
exposes `getPlayerPosition`, `setPlayerPosition`, in/out points, work-area
points and `exportAsMediaDirect` — but no preview-render or cache method at all.
The undocumented [QE DOM](https://vakago-tools.com/premiere-pro-qe-api/) is
described by Adobe as unsupported with no further work planned.

No host exposes cache *state* either, so even a lucky trigger could not be
measured or de-duplicated. A scheduler that ranks perfectly but cannot act is
not a render cache; it is a recommendation engine.

**So FrameForge inverts the relationship.** Instead of begging an application to
cache on our behalf, it schedules *your* renderer — which means it also gets to
time every render, and learns real costs for free. That sidesteps the effect
detection problem entirely.

---

## What it's for

Anything that renders frames along a timeline and caches them:

- preview/playback tooling and review players
- FFmpeg- or GStreamer-based processing pipelines
- Blender VSE add-ons, MLT-based editors, and other scriptable hosts
- proxy and pre-render generators
- notebook and batch video work where re-rendering dominates iteration time

## Install

```bash
git clone https://github.com/<you>/RenderFlow && cd RenderFlow
pip install -r requirements-dev.txt      # optional: tests + timeline ingest
```

The `frameforge` package itself is **pure standard library**. OpenTimelineIO is
only needed to read editor export formats.

## The host protocol

Only `render` is required. Implement more and FrameForge schedules better:

| Method | Effect |
|---|---|
| `render(segment)` | **Required.** Render and cache one segment. Blocking. |
| `playhead()` | Engine polls you. Otherwise push with `set_playhead()`. |
| `is_cached(segment)` | Engine trusts your cache instead of its own bookkeeping. |
| `cost_hint(segment)` | Seeds the cost model before any measurement exists. |
| `evict(segment)` | Lets the engine enforce a cache budget. |

`describe_host(host)` prints exactly which of these you have, so there are no
silent surprises about what the scheduler can and cannot do.

## Timeline ingest

FrameForge reads the interchange formats editors already export, so one path
covers all of them without any of them cooperating at runtime.

```python
from frameforge.formats import load, supported_formats
timeline = load("edit.edl")
print(supported_formats())   # which are readable in this environment
```

| Format | Written by |
|---|---|
| `.json` | FrameForge's own format — no dependencies |
| `.edl` (CMX 3600) | essentially every NLE |
| `.xml` (FCP7 XML) | Premiere, Resolve, Final Cut 7 |
| `.fcpxml` | Final Cut Pro X |
| `.aaf` | Avid Media Composer |
| `.otio`, `.kdenlive` | OpenTimelineIO, Kdenlive |

Everything but `.json` goes through OpenTimelineIO. Interchange formats carry
effect *names* inconsistently and parameters almost never — so those only seed
the cold-start estimate, which measured render times then replace.

## How priority works

For each not-yet-cached segment `c`:

```
P(c) = w_cost · C   normalised render cost      expensive work is worth caching
     + w_prox · D   proximity to the playhead   near work is needed sooner
     + w_dir  · V   playback-direction match    cache ahead of the cursor
     + w_hist · H   revisit score               sections you keep returning to
```

Every signal moves as the user works, so the priority heap is rebuilt lazily on
each `set_playhead()`. Weights live in `SchedulerConfig`.

**Multi-track flattening.** The cache works on the *composite* image at each
frame, not on individual clips. Timelines stack adjustment layers, titles and
mattes on upper tracks, so FrameForge cuts at the union of all clip boundaries
and sums the cost of everything covering each slice. Without this, one frame
range gets scheduled once per track and the stacked region — the expensive case —
reads as several cheap jobs instead of one expensive one.

**Measured cost beats estimated cost.** Because the engine calls your renderer,
it times every render and feeds the result back. Use `CostEstimator.save_profile`
/ `load_profile` to start the next session warm.

## Layout

| Path | What it is |
|---|---|
| [frameforge/host.py](frameforge/host.py) | The host protocol — the entire integration surface |
| [frameforge/engine.py](frameforge/engine.py) | `CacheEngine` — what you actually use |
| [frameforge/scheduler.py](frameforge/scheduler.py) | Priority heap and the scoring function |
| [frameforge/flatten.py](frameforge/flatten.py) | Multi-track → composite intervals |
| [frameforge/cost.py](frameforge/cost.py) | Cost model + host-neutral effect vocabulary |
| [frameforge/cache.py](frameforge/cache.py) | Cache accounting and value-based eviction |
| [frameforge/metrics.py](frameforge/metrics.py) | Dropped frames, FPS, 1% low, hit rate |
| [frameforge/formats/](frameforge/formats/) | Timeline ingest (native JSON, OTIO) |
| [frameforge/timeline.py](frameforge/timeline.py) | `Clip` / `Timeline` data model |
| [frameforge/simulation.py](frameforge/simulation.py) | Fake timelines and editor traces |

## Examples

```bash
python examples/minimal_integration.py   # start here — the smallest real host
python examples/ffmpeg_host.py --dry-run # a complete FFmpeg-backed preview cache
python examples/phase1_demo.py           # the scheduler in isolation
python examples/adaptive_demo.py         # priorities reacting to direction + revisits
python examples/benchmark.py             # none vs sequential vs adaptive
```

`ffmpeg_host.py` runs for real against a video file:

```bash
python examples/ffmpeg_host.py --input clip.mp4 --playhead 900
python examples/ffmpeg_host.py --input clip.mp4 --follow --budget 900
```

Benchmark on a non-linear editing trace with a tight render and cache budget:

| strategy | avg fps | dropped | hit rate |
|---|---|---|---|
| no cache | 2.9 | 1776 | 0.00 |
| sequential | 5.3 | 926 | 0.48 |
| **FrameForge** | **15.4** | **448** | **0.75** |

Sequential caching wins on continuous linear playback — that is the honest
result. FrameForge wins as soon as the user starts scrubbing, which is what
editing actually looks like.

## Tests

```bash
python -m pytest -q
```

## Status

Working: the scheduler, multi-track flattening, cost model with measurement
feedback, cache accounting and eviction, the host protocol, `CacheEngine` with
background warming, OTIO/EDL/FCPXML/AAF ingest, and two reference integrations.

Next: a real end-to-end benchmark against a heavy source file rather than a
simulated one, and reference hosts for Blender VSE and MLT — the two open,
fully-scriptable environments where the whole loop can be closed.
