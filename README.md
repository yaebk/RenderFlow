# RenderFlow

A performance profiler and fixer for DaVinci Resolve. It measures what is
actually slowing a project down, by how much, and applies the fixes — usable
by hand from Resolve's Scripts menu, or driven by an AI agent.

Works on the **free edition** of Resolve, which is the one that needs it most:
on Windows the free edition has no hardware H.264/H.265 decoding, so ordinary
phone and mirrorless footage stutters and most users never learn why.

## What it will do

1. **Profile** — measure, on your machine, what each clip and timeline region
   costs: decode speed per source clip, render cost per effect, settings that
   hurt playback.
2. **Fix** — apply the known remedies correctly and reversibly: proxies for the
   clips that need them (not all of them), markers on heavy effects, timeline
   settings, render-in-place for expensive regions.
3. **Report** — a plain-language summary for a human, or structured JSON and a
   tool interface for an agent.

## Design rule

Everything must be useful with no AI attached. The profiler and fixer are
deterministic code; an agent is one more front end, not the product.

## The bridge (built)

The free edition lets nothing outside Resolve use the scripting API, so
RenderFlow runs a small relay *inside* Resolve and talks to it over localhost.

```
python -m pip install -e .
```

1. Copy `scripts/RenderFlow_Bridge.py` to
   `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`
   and set `REPO` at the top of the copy to this checkout.
2. In Resolve: **Workspace -> Scripts -> RenderFlow_Bridge**. A small window
   says it is listening; leave it open.
3. From any terminal:

```
python -m renderflow.bridge
```

which prints the Resolve version, project and open timeline. In code:

```python
from renderflow import connect

resolve = connect()                      # Studio: direct. Free: via the bridge.
timeline = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
for item in timeline.GetItemListInTrack("video", 1):
    print(item.GetName(), item.GetClipProperty("Video Codec"))
```

Anything written against Blackmagic's API works unchanged over the bridge,
including passing API objects back as arguments. Only processes on the same
machine can reach it, and each request carries a per-session token from
`~/.renderflow/bridge.json`.

Tests: `python -m pytest -q` (runs against a fake Resolve; no Resolve needed).

## The scan (built)

With the bridge running:

```
python -m renderflow scan           # readable report
python -m renderflow scan --json    # for tools and agents
```

It inventories every video clip in the media pool (codec, resolution, bit
depth, length, where the file lives, proxy status), reads the open timeline
(which clips are used, real Fusion tools, colour node counts) and the
performance-related project settings, then applies explainable rules:
software-decoded long-GOP codecs, media on OneDrive/network/removable drives,
missing media, Super Scale, Fusion work, deep grades, and settings that
silently defeat proxies (for example "Prefer Camera Originals" with proxies
present). Every finding carries a plain-language *why*.

This is the unmeasured half of the profiler - rules of thumb from facts
Resolve already knows. Measured decode and render timing comes next and can
override it.

## The profiler (built)

```
python -m renderflow profile            # scan, then measure every clip
python -m renderflow profile --json
```

Needs FFmpeg (`winget install Gyan.FFmpeg` on Windows). For each source clip
it decodes a five-second sample from the middle of the file with the CPU and
times it, giving *decoded fps / clip fps* - below 1.0 the clip cannot play in
real time on this machine, below 2.0 it has no headroom for grades or
effects. It also times random-access seeks, which is what scrubbing feels
like. Measurements replace the scan's codec guesses in the findings and are
cached per file in `~/.renderflow/measurements.json`, so a project is
measured once.

FFmpeg stands in for Resolve's decoder: it is at least as fast as the free
edition's, so the ratio is an upper bound on what playback will manage.

## Render cost (built)

```
python -m renderflow render-cost            # 24-frame sample per timeline clip
python -m renderflow render-cost --frames 60
```

FFmpeg can time decoding; only Resolve can time a grade or a Fusion comp. So
this renders a short sample from the middle of every clip on the timeline
through Resolve's own render queue and reads back `TimeTakenToRenderInMs`,
giving milliseconds per frame for the whole pipeline. It reports each clip's
render speed against real time, how many times heavier it is than the
cheapest clip, what it carries (Fusion tools, grade nodes), an estimated
export time for the whole timeline, and each clip's share of it. Frames under
several tracks are charged once, to the top-most clip.

It uses the cheapest encode available (DNxHR LB) and puts everything back
afterwards: render format, page, playhead, frame range; the sample job is
deleted and its files removed; existing queue jobs are untouched.

## Status

Bridge, scan, decode profiler and render-cost profiler all verified against
a live free-edition Resolve 19. The fixer (proxies, settings, markers,
render-in-place) not started. The previous codebase (an adaptive
render-cache scheduler) is preserved in git history at `50a4854`.
