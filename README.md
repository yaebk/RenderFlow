# RenderFlow

A performance profiler and fixer for DaVinci Resolve.

It measures what is slow in the open project (source decode speed, render
cost per clip), reports it with numbers, and can apply the fixes that the
numbers justify: proxies, cache settings, markers. Every change is logged and
can be undone.

It works on the free edition of Resolve. On Windows the free edition has no
hardware H.264/H.265 decoding, so ordinary phone and camera footage often
stutters and it is not obvious why.

## Setup

Requires Python 3.8+ and FFmpeg (`winget install Gyan.FFmpeg` on Windows).
The part that runs inside Resolve needs whatever Python Resolve embeds to be
3.7 or newer.

```
python -m pip install -e .
```

This also installs a `renderflow` command; `renderflow report` and
`python -m renderflow report` are the same thing (the examples below use the
second form, which works even when pip's scripts folder is not on your PATH).

The free edition does not let outside programs use Resolve's scripting API,
so RenderFlow runs a small relay script inside Resolve and talks to it over
localhost:

1. Put the launcher in Resolve's scripts menu:

   ```
   python -m renderflow install-bridge
   ```

   This copies `scripts/RenderFlow_Bridge.py` into
   `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`
   (or the macOS/Linux equivalent) with the path of this checkout filled in.
2. In Resolve, open a project and go to **Workspace > Scripts > RenderFlow_Bridge**.
   A small window opens and says it is listening. Leave it open.
3. Check the connection from a terminal:

   ```
   python -m renderflow.bridge
   ```

   This prints the Resolve version, the project and the current timeline.

On Resolve Studio the bridge is not needed; RenderFlow connects directly.

## Usage

```
python -m renderflow report          # measure the open project and list what is slow
python -m renderflow fix             # show what would change (nothing changes)
python -m renderflow fix --apply     # make the changes
python -m renderflow fix --undo      # reverse them
```

`report` runs three measurements and prints ranked findings plus a plan.
`report --json` gives the same thing as JSON. `--no-render` skips the
render-queue measurement, which is the slow part (roughly 15-30 s per
timeline clip the first time; after that the samples come from a cache).
`--remeasure` ignores the caches.

Each measurement can also be run on its own:

```
python -m renderflow scan            # inventory and rules of thumb, no measuring
python -m renderflow profile         # scan plus FFmpeg decode timing
python -m renderflow render-cost     # render a sample of each timeline clip
python -m renderflow tools           # which Fusion tool costs what, on the clips that render heavy
```

`report --tools` folds the last one into the full report.

## What it measures

**Scan.** Every video clip in the media pool: codec, resolution, bit depth,
length, where the file lives (local, OneDrive, network, removable, missing),
and whether it has a proxy. The current timeline: which clips are used, how
many real Fusion tools and colour nodes each has. The performance settings:
proxy mode, render cache, Super Scale. From this it applies rules of thumb:
long-GOP codecs decoded in software (on the free edition; Studio decodes them
on the GPU, so there they are only noted), media on synced or network drives,
settings that quietly disable proxies, and so on. Each finding says why it
matters. These are guesses from facts Resolve already knows, and the two
measurements below override them.

**Decode.** FFmpeg decodes a five-second sample from the middle of each source
file on the CPU. The result is decoded fps divided by the clip's fps. Below
1.0 the clip cannot play in real time on this machine; below 2.0 there is no
headroom for grades or effects; above that the codec is not the problem. It
also times random seeks, which is what scrubbing feels like. Results are cached
per file in `~/.renderflow/measurements.json`.

**Render cost.** FFmpeg can time decoding, but only Resolve can time a grade or
a Fusion comp. So RenderFlow renders a short and a long sample of each timeline
clip through Resolve's own render queue and takes the slope between them,
which removes the fixed per-job overhead and gives true milliseconds per frame
for the whole pipeline. It reports each clip's render speed against real time,
its share of total export time, and an estimated export time for the
timeline.

Clips under 120 frames cannot be measured on their own: below that Resolve's
per-job set-up time swamps the per-frame cost and a single sample would read
as a heavy clip. Runs of them - a fast-cut section - are measured as
*stretches* instead: the frames covered only by short clips, in pieces of
about ten seconds, each rendered like a clip. A stretch's number is the
average over the clips it spans, and its finding and marker cover the whole
run. A short clip with no short neighbours stays "too short to measure".

It uses the cheapest encoder available (DNxHR LB), deletes the sample jobs and
files, and restores the render format, page, playhead and range afterwards.
Existing queue jobs are not touched. Samples are cached in
`~/.renderflow/render.json` so that `report` followed by `fix` does not render
everything twice. The cache key covers the clip, its position and length, its
Fusion tools and grade node count, and the timeline format; it does not see a
changed parameter inside an existing effect, so use `--remeasure` after that.

**Fusion tools.** When a clip renders below real time and carries Fusion
tools, `tools` finds out which tool is responsible: it bypasses each one in
turn (the node's own pass-through switch), renders the same frames again, and
reports what each tool's absence saves, per frame. Comps that are
tool-for-tool identical (a macro dropped on seven clips) are measured once.
Resolve reports job time in half-second steps, so each figure carries a
stated +/- and the savings overlap rather than add up; the ranking is what
to trust. Nothing is deleted or edited: only the pass-through flag is
touched, only on tools that were on, each is put back right after its
sample and read back to confirm, and the tools currently bypassed are listed
in `~/.renderflow/bypassed.json` so a run killed halfway is repaired by the
next run or by `fix --undo`. A tool the comp cannot render without (a Text+
template, the OpticalFlow feeding a TimeStretcher) is reported as such.

## What it fixes

Only what the measurements support:

- DNxHR LB proxies for clips that decode below real time, generated with
  FFmpeg into `~/Videos/RenderFlow Proxies` and linked in the media pool,
  along with the *Prefer Proxies* setting Resolve needs to actually use them.
- Render Cache set to Smart when a clip renders heavy and carries effects.
- Super Scale turned off.
- Timeline markers over clips with high or medium findings: red for high,
  yellow for medium, the finding in the note. Resolve allows one marker per
  frame, so clips on different tracks that start together share one, and a
  frame that already has a marker of your own is left alone (the plan says
  which).

`fix` alone prints the plan and changes nothing. `fix --apply` writes a
journal to `~/.renderflow/journal.json` as it goes; `fix --undo` walks it
backwards for the project that is open (changes to another project wait until
you open it), leaves any setting you have changed by hand since, and also
clears any RenderFlow marker still on the timeline even if the journal is gone
(they are tagged). `--proxies all` forces proxies for every
long-GOP clip; `--proxies none`, `--no-markers` and `--no-settings` narrow the
plan; `--no-render` plans without the render measurement.

Render-in-place is deliberately not included. Smart cache covers the same
case without freezing the content.

## Example

On a Windows machine running Resolve 19 free edition, a plain 1080p60 H.264
screen capture decoded at 375 fps (6x real time) and rendered at 3-4 ms per
frame. The scan's rule of thumb had flagged it as slow; the measurement
overrode that and no proxy was proposed. A copy of the same clip with five
chained Fusion blurs rendered at 25 ms per frame (0.67x real time, 66% of
export time). For that one the tool proposed Smart cache and a marker, applied
them, and undid them cleanly.

## Using it from code

```python
from renderflow import connect

resolve = connect()
timeline = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
for item in timeline.GetItemListInTrack("video", 1):
    print(item.GetName(), item.GetClipProperty("Video Codec"))
```

`connect()` returns Resolve's normal scripting object, over the bridge on the
free edition or directly on Studio. Anything written against Blackmagic's API
works unchanged, including passing API objects back as arguments. The bridge
only accepts connections from the same machine and each request carries a
per-session token from `~/.renderflow/bridge.json`.

There is also a Claude Code skill in `.claude/skills/renderflow` that runs
`report --json` and explains the result. The tool does not depend on it.

## Tests

```
python -m pytest -q
```

The tests run against a fake Resolve and do not need Resolve installed. One
decode test needs FFmpeg and is skipped without it.

## Limits

Resolve exposes no playback timing, so there are no before/after playback
numbers; decode and render measurements stand in for them. The render sample
includes an encode, so playback is a little faster than the render ratio
suggests, but the ranking between clips holds.

On a fast-cut timeline the render numbers come from stretches, so they say
which ten seconds are heavy, not which cut; the scan's per-clip effect
findings narrow it down from there.

Studio's direct connection is implemented but has only been tested against
the free edition.

## Authors

- [HudeiCS](https://github.com/HudeiCS)
- [yaebk](https://github.com/yaebk)

Pair-programmed on a shared machine; commits are under a single account.
