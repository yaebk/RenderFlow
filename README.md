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

## Status

Bridge done and tested against a fake Resolve; not yet run against a live
Resolve. Profiler and fixer not started. The previous codebase (an adaptive
render-cache scheduler) is preserved in git history at `50a4854`.
