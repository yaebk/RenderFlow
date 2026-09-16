---
name: renderflow
description: Diagnose and fix DaVinci Resolve performance problems (stuttering playback, slow exports) by measuring the open project with RenderFlow, then explaining the findings and applying reversible fixes. Use when the user mentions Resolve being slow, laggy, dropping frames, stuttering, or asks how long an export will take.
---

# RenderFlow: Resolve performance profiler and fixer

RenderFlow measures the project that is open in DaVinci Resolve and reports
what is slow, why, and what to change. Everything it does is deterministic
and reversible; your job is to run it, read the JSON, explain the result in
plain language, and apply fixes only with the user's agreement.

## Preconditions

1. Resolve is open with a project and (usually) a timeline.
2. The bridge is running inside Resolve: **Workspace -> Scripts -> RenderFlow_Bridge**
   (free edition; on Studio the tool attaches directly). Check with:
   ```
   python -m renderflow.bridge
   ```
   If it says no discovery file, ask the user to start the bridge. If the menu
   entry is missing, `python -m renderflow install-bridge` puts it there. Do
   not try to work around the bridge.
3. FFmpeg on PATH (`winget install Gyan.FFmpeg` on Windows). Without it the
   decode measurement is skipped and the report says so.

Run every command from the RenderFlow repo directory with `python -m renderflow ...`.

## The one command to start with

```
python -m renderflow report --json
```

Runs everything: inventory, FFmpeg decode measurement per clip, a render-cost
sample of every timeline clip through Resolve's own render queue (roughly
15-30 s per clip the first time; both measurements are cached, `--remeasure`
ignores the caches), and a plan of fixes. Nothing is changed. Read:

- `findings[]` - ranked `high` / `medium` / `info`, each with `code`,
  `subject` (a clip name, a timeline item as `name @V1 01:00:17:09`, or
  `project`), `message`, and `why`. Quote `why` when explaining.
- `scan.clips[].measured` - decode fps and `realtime_ratio` (< 1 cannot play
  in real time; < 2 no headroom; >= 2 decode is not the problem).
- `render.samples[]` - `ms_per_frame`, `realtime_ratio`, `export_share`,
  `fusion_tools`, `color_nodes`, `from_cache`; `render.estimated_export_s`
  for the whole timeline. A sample with `status: "Too short"` is a clip under
  120 frames, which cannot be measured alone (Resolve's per-job set-up time
  would make it read as heavy); its `in_stretch` names the stretch that
  measured it. A sample with `stretch: true` is a run of such clips measured
  together, `clips` listing them: its number is the average over the run, so
  say "these ten seconds" rather than blaming one cut. A short clip with an
  empty `in_stretch` had no short neighbours and is not measured at all.
- `actions[]` - what `fix --apply` would do, with `estimate_s` for encodes.
- `notes[]` - things about the plan that are not actions, e.g. findings that
  could not get a marker because the frame already has one.
- `skipped[]` - stages that did not run and why.

For a quick look without rendering (seconds instead of minutes):
`python -m renderflow report --json --no-render`.

When a clip renders below real time and carries Fusion tools, find out which
tool is to blame: `python -m renderflow tools --json` (or `report --tools`).
It bypasses each tool in turn and re-renders; read `comps[].tools[]` for
`saved_ms_per_frame` per tool (a `status` other than Complete means the comp
cannot render without that tool). Quote the ranking, not the decimals - the
report states a +/- and the savings overlap. It changes nothing: every tool
is put back and checked; `problems[]` must be empty, and if it is not, tell
the user which tool to check in Fusion.

## Applying fixes

```
python -m renderflow fix            # prints the plan, changes nothing
python -m renderflow fix --apply    # makes the changes, journaled
python -m renderflow fix --undo     # reverses every journaled change
python -m renderflow fix --tools    # also measures Fusion tools and plans bypassing the costly ones
python -m renderflow fix --restore-tools   # re-enables bypassed tools only (before delivery)
```

Fix kinds: proxies (DNxHR LB via FFmpeg, linked with `LinkProxyMedia`, plus
Playback -> Proxy Handling -> Prefer Proxies, without which Resolve ignores
them); settings (Render Cache -> Smart when a clip measured heavy and carries
effects; Super Scale off); timeline markers over clips with high/medium
findings (red = high, yellow = medium, one per frame; a frame that already
has the user's own marker is left alone); tool bypass (with `--tools`: the
Fusion tools measured as the cost of a below-real-time comp are switched to
pass-through while editing - nothing deleted, effect off until restored).
`--proxies all` forces proxies for every long-GOP clip; `--no-markers` /
`--no-settings` / `--no-bypass` narrow the plan; `--no-render` plans without
the render measurement.

A `fusion-tools-bypassed` finding (always high) means an earlier apply left
tools off. Tell the user plainly which clips, that the effect is missing from
playback and from any export, and that `fix --restore-tools` puts them back
without undoing proxies or settings. Never let a delivery go out with it
showing.

Always show the user the plan and get a yes before `--apply`. Tell them
`--undo` exists: it reverses the journal for the project that is open, leaves
any setting the user changed by hand since, and removes every RenderFlow
marker on the timeline even if the journal is gone. Proxies take about a
quarter of the footage's duration to encode and use disk space under
`~/Videos/RenderFlow Proxies`.

## How to explain results

- Lead with the measured numbers, not the codec folklore. "Your H.264 decodes
  at 375 fps here, 6x real time - the codec is not the problem" is the kind
  of sentence the tool exists to make possible.
- Decode ratio answers "is the source footage the bottleneck". Render ratio
  answers "is the grade / Fusion / scaling the bottleneck". If decode is fine
  and render is slow, it is the effects.
- `export_share` says where export time goes; one clip at 60%+ is the thing
  to simplify or pre-render.
- The render sample includes an encode, so playback is somewhat faster than
  the render ratio suggests; the ranking between clips is reliable.
- When the plan is empty, say so: the measurements do not justify changes,
  and guessing would make things worse.

## What it cannot do

Read pixels, control Resolve's own render cache beyond the mode setting, or
measure playback directly (Resolve exposes no playback timing). It does not
bake clips to files (render-in-place); Smart Render Cache covers that case
without freezing content.
