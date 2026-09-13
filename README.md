# RenderFlow

A performance profiler and fixer for DaVinci Resolve. It measures what is
actually slowing a project down, by how much, and applies the fixes — usable
by hand from Resolve's Scripts menu, or driven by an AI agent.

Works on the **free edition** of Resolve, which is the one that needs it most:
on Windows the free edition has no hardware H.264/H.265 decoding, so ordinary
phone and mirrorless footage stutters and most users never learn why.

## What it does

```
python -m renderflow report          # measure the open project, rank what is slow, plan fixes
python -m renderflow fix             # show the plan (nothing changes)
python -m renderflow fix --apply     # make the changes, journaled
python -m renderflow fix --undo      # reverse them
```

Three instruments, one report:

1. **Scan** - inventory of every clip (codec, resolution, bit depth, where
   the file lives, proxies), the timeline (which clips, real Fusion tools,
   colour nodes), and the performance settings; plus explainable rules of
   thumb.
2. **Decode profiler** - FFmpeg decodes a sample of every source clip on this
   machine: *decoded fps / clip fps*. Below 1 the clip cannot play in real
   time; above 2 the codec is not the problem. Also times random-access
   seeks (scrubbing feel).
3. **Render-cost profiler** - Resolve's own render queue renders a sample of
   every timeline clip; the slope between a short and a long sample gives
   true milliseconds per frame for the whole pipeline (decode, grade,
   Fusion, scaling). Reports each clip's cost against real time, against the
   cheapest clip, its share of export time, and an estimated export time for
   the timeline.

Then the **fixer** applies only what the measurements justify, journaled and
reversible: DNxHR LB proxies (and the *Prefer Proxies* setting Resolve needs
to use them), Render Cache -> Smart for effect-heavy clips, Super Scale off,
and timeline markers over the clips with findings.

Measured on a real project (Resolve 19, free edition, Windows): a plain
1080p60 H.264 capture decodes at 375 fps and renders at 3-4 ms/frame - the
rule-of-thumb "H.264 is slow" was wrong for that machine and the tool said
so; a copy of the same clip with five chained Fusion blurs renders at
25 ms/frame, 0.67x real time, 6x the cheapest clip, 66% of export time - and
the tool proposed Smart cache and a marker, applied them, and undid them.

## Design rules

Everything is useful with no AI attached. The profiler and fixer are
deterministic code; an agent is one more front end. The `.claude/skills/
renderflow` skill teaches Claude Code to run `report --json` and explain it.

Measure, don't guess. Every rule-of-thumb finding is labelled as such and is
replaced by a measurement when one exists.

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

Bridge, scan, decode profiler, render-cost profiler, fixer and undo all
verified against a live free-edition Resolve 19, including a four-clip test
timeline with real Fusion effects. Not done: render-in-place (deliberately -
Smart cache covers it without freezing content), an MCP server (the skill
file plus `--json` covers Claude Code), and before/after playback numbers
(Resolve exposes no playback timing; decode and render measurements are the
proxy for it). The previous codebase (an adaptive
render-cache scheduler) is preserved in git history at `50a4854`.
