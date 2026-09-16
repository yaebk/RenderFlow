"""Measure what each timeline clip costs Resolve to render, using Resolve itself.

FFmpeg can time decoding, but only Resolve can time a grade, a Fusion comp or
a Super Scale. So this renders two samples from the middle of every clip on
the timeline through the render queue - a short one (2 s) and a long one
(10 s) - and reads back ``TimeTakenToRenderInMs`` from each job. That time
includes a fixed per-job set-up cost (about 0.7 s on a live Resolve 19) and
the pipeline is deeply parallel, so small jobs all take the same time and a
single sample badly overstates the per-frame cost; the slope between the two
samples is the true milliseconds per frame for the whole pipeline - decode,
colour, Fusion, scaling - plus the encode of the sample.

    ms_per_frame   = (t_long - t_short) / (frames_long - frames_short)
    realtime_ratio = frames rendered per second / timeline fps

Clips under 120 frames cannot be measured on their own: at that size the
pipeline's parallelism hides the slope. Runs of such clips - a fast-cut
section - are measured as *stretches* instead: the frames of the timeline
covered by short clips and by no long one, in pieces of about the long sample
length. A stretch's number is the average over the clips it spans.

WHAT THE NUMBER MEANS
---------------------
It is a real export speed for the sample codec (DNxHR LB, the cheapest encode
Resolve offers), so the per-clip estimate of *export* time is honest. For
*playback*, which does not encode, the absolute number is pessimistic, but the
ranking between clips is right: a clip that renders three times slower than
its neighbours is three times heavier to play too.

WHAT IT TOUCHES, AND RESTORES
-----------------------------
Rendering through the API switches Resolve to the Deliver page, moves the
playhead, and changes the current render format and range. All of that is
put back when the run ends, the sample job is deleted from the queue, and
the output files are removed. Any render jobs already in the queue are left
alone. It refuses to start if a render is already in progress.

The Deliver page renders from the project's Render Cache when it is on and
built, so with Smart or User cache the numbers say how the timeline plays
*from cache*; with it off they are the raw cost of the effects. The report
says which; the plan proposes Smart cache only from raw numbers.

Results are cached in ~/.renderflow/render.json, keyed by everything that
should change the number: the item (source file, position, length, Fusion
tools, grade node count), the timeline's format, the Render Cache mode, the
sample codec and lengths, and the CPU. A parameter tweak inside an effect
does not change the key, so ``--remeasure`` forces a fresh run.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from renderflow.scan import (
    Finding,
    ScanReport,
    _fusion_tools,
    _int,
    _node_count,
    frames_to_timecode,
    item_label,
    sort_findings,
)

DEFAULT_SECONDS = 10.0          # long sample, in timeline seconds
DEFAULT_SHORT_SECONDS = 2.0     # short sample; slope between the two cancels job set-up
DEFAULT_BUDGET_S = 60.0         # cap on wall time per long sample
MIN_LONG_FRAMES = 120           # below this the pipeline's parallelism hides the slope
TOO_SHORT = "Too short"         # status of a clip under MIN_LONG_FRAMES: not measured on its own
SAMPLE_NAME = "renderflow_sample"
RENDER_CACHE_PATH = Path.home() / ".renderflow" / "render.json"
RENDER_CACHE_VERSION = 1
PREFERRED = [("mov", "DNxHRLB"), ("mov", "DNxHRSQ"), ("mp4", "H264")]
JOB_TIMEOUT_S = 900.0


@dataclass
class RenderSample:
    item: str
    track: int
    start: int                  # timeline frames, end exclusive
    end: int
    sample_start: int
    frames: int
    render_ms: float
    status: str                 # Complete | Failed | Cancelled | Timeout
    fusion_tools: list[str] = field(default_factory=list)
    color_nodes: int = 0
    short_frames: int = 0       # second, shorter sample used to cancel per-job overhead
    short_ms: float = 0.0
    label: str = ""             # unique identity: "<name> @V<track> <timecode>"
    measured_at: float = 0.0    # time.time() of the render
    from_cache: bool = False    # reused from an earlier run
    clips: list[str] = field(default_factory=list)   # a stretch: labels of the clips it spans
    in_stretch: str = ""        # a too-short clip: label of the stretch that measured it

    def __post_init__(self) -> None:
        if not self.label:
            self.label = f"{self.item} @V{self.track} #{self.start}"

    @property
    def ok(self) -> bool:
        return self.status == "Complete" and self.frames > 0 and self.render_ms > 0

    @property
    def too_short(self) -> bool:
        return self.status == TOO_SHORT

    @property
    def stretch(self) -> bool:
        return bool(self.clips)

    @property
    def two_point(self) -> bool:
        return self.ok and 0 < self.short_frames < self.frames and self.short_ms > 0

    @property
    def overhead_ms(self) -> float:
        """Fixed per-job cost implied by the two samples (0 if only one sample)."""
        if not self.two_point:
            return 0.0
        return max(0.0, self.short_ms - self.short_frames * self.ms_per_frame)

    @property
    def ms_per_frame(self) -> float:
        if not self.ok:
            return 0.0
        if self.two_point:
            slope = (self.render_ms - self.short_ms) / (self.frames - self.short_frames)
            if slope > 0:
                return slope
        return self.render_ms / self.frames

    @property
    def render_fps(self) -> float:
        return 1000.0 / self.ms_per_frame if self.ok else 0.0

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class RenderProfile:
    timeline: str
    fps: float
    format: str
    codec: str
    samples: list[RenderSample]
    estimated_export_s: float = 0.0
    shares: dict[str, float] = field(default_factory=dict)   # sample label -> share of export time
    total_frames: int = 0
    render_cache: str = "none"      # perfRenderCacheMode while measuring: none | smart | user

    @property
    def from_cache_mode(self) -> bool:
        """True if Resolve may have rendered the samples from its Render Cache."""
        return self.render_cache not in ("", "none")

    def ratio(self, sample: RenderSample) -> float:
        return sample.render_fps / self.fps if self.fps and sample.ok else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for sample, raw in zip(self.samples, data["samples"]):
            raw["ms_per_frame"] = round(sample.ms_per_frame, 2)
            raw["overhead_ms"] = round(sample.overhead_ms, 1)
            raw["render_fps"] = round(sample.render_fps, 2)
            raw["realtime_ratio"] = round(self.ratio(sample), 3)
            raw["export_share"] = round(self.shares.get(sample.label, 0.0), 4)
            raw["stretch"] = sample.stretch
        return data

    def text(self) -> str:
        overheads = [s.overhead_ms for s in self.samples if s.two_point]
        note = (f", per-job overhead ~{sum(overheads) / len(overheads):.0f} ms removed"
                if overheads else "")
        lines = [f"timeline : {self.timeline} @ {self.fps:g} fps, sample codec "
                 f"{self.format}/{self.codec}, Render Cache {self.render_cache or 'none'}{note}",
                 f"{'clip':<34} {'trk':>3} {'frames':>7} {'ms/frame':>9} {'render':>8} "
                 f"{'ratio':>6} {'export':>7}  carries"]
        for s in self.samples:
            if s.stretch:
                name, track = s.label, "-"
            else:
                name = (s.item[:21] + "...") if len(s.item) > 24 else s.item
                name, track = f"{name} @V{s.track} {s.label.rsplit(' ', 1)[-1]}", str(s.track)
            carries = []
            if s.fusion_tools:
                carries.append("fusion " + ",".join(sorted(set(s.fusion_tools))[:3]))
            if s.color_nodes > 1:
                carries.append(f"{s.color_nodes} nodes")
            if not s.ok:
                if s.in_stretch:
                    status = "measured in a stretch (below)"
                else:
                    status = "too short to measure" if s.too_short else s.status
                lines.append(f"{name:<34} {track:>3} {s.length:>7} {'-':>9} {'-':>8} {'-':>6} "
                             f"{'-':>7}  {status}")
                continue
            share = self.shares.get(s.label, 0.0)
            lines.append(f"{name:<34} {track:>3} {s.length:>7} {s.ms_per_frame:>9.1f} "
                         f"{s.render_fps:>7.1f}  {self.ratio(s):>5.2f}x {share:>6.0%}  "
                         f"{', '.join(carries)}")
        cached = [s for s in self.samples if s.from_cache]
        if cached:
            oldest = min(s.measured_at for s in cached)
            lines.append("")
            lines.append(f"{len(cached)} sample(s) reused from an earlier run ({_age(oldest)} ago); "
                         "--remeasure renders them again.")
        short = sum(1 for s in self.samples if s.too_short)
        if short:
            lines.append("")
            lines.append(self._short_note(short))
        if self.from_cache_mode:
            lines.append("")
            lines.append(f"Render Cache is {self.render_cache}: clips it has already cached render from "
                         "the cache here, so these numbers are how the timeline plays once the cache "
                         "is built, not what the effects cost. Set it to None and --remeasure for that.")
        if self.estimated_export_s:
            minutes, seconds = divmod(int(round(self.estimated_export_s)), 60)
            lines.append("")
            lines.append(f"estimated export of the whole timeline at {self.codec}: "
                         f"{minutes}m {seconds:02d}s for {self.total_frames} frames "
                         f"({self.total_frames / self.fps:.0f}s of video)")
        return "\n".join(lines)


    def _short_note(self, short: int) -> str:
        covered = sum(1 for s in self.samples if s.too_short and s.in_stretch)
        stretches = sum(1 for s in self.samples if s.stretch)
        why = (f"Resolve's per-job set-up time hides the per-frame cost of anything under "
               f"{MIN_LONG_FRAMES} frames")
        if not covered:
            return f"{short} clip(s) under {MIN_LONG_FRAMES} frames not measured: {why}."
        text = (f"{covered} clip(s) under {MIN_LONG_FRAMES} frames measured as {stretches} "
                f"stretch(es) of consecutive clips: {why}, so a stretch's number is the average "
                "over the clips it spans.")
        if covered < short:
            text += (f" {short - covered} clip(s) not measured: no neighbours to make up "
                     f"{MIN_LONG_FRAMES} frames.")
        return text


def _age(when: float) -> str:
    seconds = max(0.0, time.time() - when)
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.0f} days"


# ------------------------------------------------------------------ cache
class RenderCache:
    """Completed samples from earlier runs. ``RenderCache(None)`` keeps nothing on disk."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.data: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if loaded.get("version") == RENDER_CACHE_VERSION:
                    self.data = loaded.get("entries", {})
            except (OSError, ValueError):
                self.data = {}

    def get(self, key: str) -> RenderSample | None:
        raw = self.data.get(key)
        if not raw:
            return None
        try:
            sample = RenderSample(**raw)
        except TypeError:
            return None
        sample.from_cache = True
        return sample

    def put(self, key: str, sample: RenderSample) -> None:
        if not sample.ok:
            return
        self.data[key] = {**asdict(sample), "from_cache": False}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"version": RENDER_CACHE_VERSION, "entries": self.data},
                                            indent=1), "utf-8")


def _render_key(item: dict, timeline_fp: str, seconds: float, short_seconds: float) -> str:
    raw = "|".join([
        item.get("path", ""), item["name"], str(item["track"]), str(item["start"]), str(item["end"]),
        ",".join(item["fusion_tools"]), str(item["color_nodes"]),
        timeline_fp, f"{seconds:g}", f"{short_seconds:g}", platform.processor(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ the queue
class RenderQueue:
    """One-sample renders through Resolve's queue, with everything put back after."""

    def __init__(self, resolve, target_dir: str | None = None, fmt: str | None = None,
                 codec: str | None = None, poll_s: float = 0.05, timeout_s: float = JOB_TIMEOUT_S):
        self.resolve = resolve
        self.project = resolve.GetProjectManager().GetCurrentProject()
        self.timeline = self.project.GetCurrentTimeline()
        self.target_dir = target_dir                    # made on enter when None
        self._own_dir = target_dir is None
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.format, self.codec = (fmt, codec) if fmt and codec else choose_format(self.project)
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> "RenderQueue":
        if self.project.IsRenderingInProgress():
            raise RuntimeError("Resolve is already rendering - wait for it to finish first")
        self._saved = {
            "format": self.project.GetCurrentRenderFormatAndCodec() or {},
            "page": self.resolve.GetCurrentPage(),
            "timecode": self.timeline.GetCurrentTimecode(),
        }
        if not self.project.SetCurrentRenderFormatAndCodec(self.format, self.codec):
            self._saved = {}
            raise RuntimeError(f"Resolve refused render format {self.format}/{self.codec}")
        if self._own_dir:
            self.target_dir = tempfile.mkdtemp(prefix="renderflow_")
        os.makedirs(self.target_dir, exist_ok=True)
        return self

    def __exit__(self, *exc: object) -> None:
        self.restore()

    def restore(self) -> None:
        saved, self._saved = self._saved, {}
        if not saved:
            return
        fmt = saved["format"]
        if fmt.get("format") and fmt.get("codec"):
            self.project.SetCurrentRenderFormatAndCodec(fmt["format"], fmt["codec"])
        self.project.SetRenderSettings({"SelectAllFrames": True})
        if saved.get("page"):
            self.resolve.OpenPage(saved["page"])
        if saved.get("timecode"):
            self.timeline.SetCurrentTimecode(saved["timecode"])
        if self._own_dir and self.target_dir:
            shutil.rmtree(self.target_dir, ignore_errors=True)

    def render_range(self, mark_in: int, mark_out: int, name: str = SAMPLE_NAME) -> tuple[float, str]:
        """Render [mark_in, mark_out] inclusive. Returns (milliseconds, status)."""
        ok = self.project.SetRenderSettings({
            "SelectAllFrames": False, "MarkIn": int(mark_in), "MarkOut": int(mark_out),
            "TargetDir": self.target_dir, "CustomName": name,
            "ExportVideo": True, "ExportAudio": False,
        })
        if not ok:
            raise RuntimeError(f"SetRenderSettings failed for frames {mark_in}-{mark_out}")
        job = self.project.AddRenderJob()
        if not job:
            raise RuntimeError("AddRenderJob failed")
        started = time.perf_counter()
        status = "Failed"
        try:
            if not self.project.StartRendering([job], False):
                raise RuntimeError("StartRendering failed")
            deadline = started + self.timeout_s
            while self.project.IsRenderingInProgress():
                if time.perf_counter() > deadline:
                    self.project.StopRendering()
                    status = "Timeout"
                    break
                time.sleep(self.poll_s)
            wall_ms = (time.perf_counter() - started) * 1000.0
            info = self.project.GetRenderJobStatus(job) or {}
            if status != "Timeout":
                status = str(info.get("JobStatus") or "Unknown")
            reported = info.get("TimeTakenToRenderInMs")
            ms = float(reported) if reported else wall_ms
            return ms, status
        finally:
            try:
                self.project.DeleteRenderJob(job)
            except Exception:
                pass
            self._remove_outputs(name)

    def _remove_outputs(self, name: str) -> None:
        try:
            for entry in os.listdir(self.target_dir):
                if entry.startswith(name):
                    try:
                        os.remove(os.path.join(self.target_dir, entry))
                    except OSError:
                        pass
        except OSError:
            pass


def choose_format(project) -> tuple[str, str]:
    """Cheapest encode Resolve offers here, so the sample measures processing, not encoding."""
    for fmt, codec in PREFERRED:
        try:
            codecs = project.GetRenderCodecs(fmt) or {}
        except Exception:
            continue
        if codec in codecs.values():
            return fmt, codec
    current = project.GetCurrentRenderFormatAndCodec() or {}
    if current.get("format") and current.get("codec"):
        return current["format"], current["codec"]
    raise RuntimeError("no usable render format found")


# ------------------------------------------------------------- measuring
def timeline_items(timeline) -> list[dict[str, Any]]:
    """Every non-empty item on every video track, with what it carries."""
    items = []
    for track in range(1, _int(timeline.GetTrackCount("video")) + 1):
        for item in timeline.GetItemListInTrack("video", track) or []:
            start, end = _int(item.GetStart()), _int(item.GetEnd())
            if end <= start:
                continue
            items.append({
                "name": str(item.GetName()), "track": track, "start": start, "end": end,
                "fusion_tools": _fusion_tools(item), "color_nodes": _node_count(item),
                "path": _media_path(item),
            })
    return items


def _media_path(item) -> str:
    try:
        media = item.GetMediaPoolItem()
        return str(media.GetClipProperty("File Path") or "") if media is not None else ""
    except Exception:
        return ""


def _label(item: dict, fps: float) -> str:
    return item.get("label") or item_label(item["name"], item["track"], item["start"], fps)


def plan_stretches(items: list[dict], fps: float, seconds: float = DEFAULT_SECONDS,
                   min_frames: int = MIN_LONG_FRAMES) -> list[dict]:
    """Ranges of the timeline covered only by clips too short to measure alone,
    cut into pieces of about ``seconds`` (never under ``min_frames``), each
    described like a timeline item so it can be sampled and cached the same way.

    A piece carries the union of its clips' Fusion tools, their deepest grade,
    and ``clips``: the labels of everything it spans. Its ``path`` encodes
    every clip's file and position, so any re-cut changes the cache key.
    """
    short = [i for i in items if i["end"] - i["start"] < min_frames]
    long_cover = _merge([(i["start"], i["end"]) for i in items if i["end"] - i["start"] >= min_frames])
    ranges: list[tuple[int, int]] = []
    for a, b in _merge([(i["start"], i["end"]) for i in short]):
        ranges.extend(_subtract((a, b), long_cover))
    target = max(min_frames, int(round(seconds * fps)))
    out = []
    for a, b in _merge(ranges):
        if b - a < min_frames:
            continue
        pieces = max(1, (b - a) // target)
        edges = [a + (b - a) * k // pieces for k in range(pieces + 1)]
        for x, y in zip(edges, edges[1:]):
            members = [i for i in items if i["start"] < y and i["end"] > x]
            out.append({
                "name": f"stretch of {len(members)} clips", "track": 0, "start": x, "end": y,
                "label": f"stretch @ {frames_to_timecode(x, fps)} ({len(members)} clips)",
                "fusion_tools": sorted({t for m in members for t in m["fusion_tools"]}),
                "color_nodes": max((m["color_nodes"] for m in members), default=0),
                "path": ";".join(f"{m.get('path', '')}@{m['start']}-{m['end']}" for m in members),
                "clips": [_label(m, fps) for m in members],
            })
    return out


def sample_sizes(length: int, fps: float, seconds: float, short_seconds: float,
                 min_frames: int = MIN_LONG_FRAMES) -> tuple[int, int]:
    """(long, short) sample lengths in frames for a clip of ``length`` frames.

    Resolve's render pipeline is deeply parallel: on a live Resolve 19 a
    12-frame and a 48-frame job took the same time, and the true per-frame
    slope only appeared past ~100 frames. So the long sample is at least
    ``min_frames`` and the short one at most a quarter of it.
    """
    long = max(min_frames, int(round(seconds * fps)))
    long = max(1, min(long, length))
    short = int(round(short_seconds * fps)) if short_seconds > 0 else 0
    short = min(short, long // 4)
    if short < 4:
        short = 0
    return long, short


def render_cost(resolve, seconds: float = DEFAULT_SECONDS, short_seconds: float = DEFAULT_SHORT_SECONDS,
                budget_s: float = DEFAULT_BUDGET_S, progress: Callable[[str], None] | None = None,
                queue: RenderQueue | None = None, cache: RenderCache | None = None) -> RenderProfile:
    """Sample every clip on the current timeline and return a :class:`RenderProfile`.

    Each clip gets a short sample first, then a long one whose length is cut
    back if the short one predicts it would take more than ``budget_s`` of
    wall time (a heavy Fusion clip at seconds per frame). The slope between
    the two cancels the fixed per-job cost. Clips shorter than
    ``MIN_LONG_FRAMES`` are not rendered on their own: a single sample of one
    would be mostly set-up time and read as a heavy clip. Runs of them are
    rendered as stretches instead (:func:`plan_stretches`), listed after the
    clips. Samples already in ``cache``
    (default: the on-disk one) are reused instead of rendered. The queue is
    only entered - render format set, Deliver page shown - if something
    actually has to be rendered.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise RuntimeError("no project is open in Resolve")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise RuntimeError("no timeline is open in Resolve")
    fps = float(timeline.GetSetting("timelineFrameRate") or 0) or 24.0
    items = timeline_items(timeline)
    queue = queue or RenderQueue(resolve)
    cache = cache if cache is not None else RenderCache(RENDER_CACHE_PATH)
    timeline_fp = "|".join(str(timeline.GetSetting(k) or "") for k in
                           ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight"))
    render_cache = str(project.GetSetting("perfRenderCacheMode") or "none")
    timeline_fp += f"|{queue.format}/{queue.codec}|cache={render_cache}"

    samples: list[RenderSample] = []
    stretches = plan_stretches(items, fps, seconds)
    short_clips = sum(1 for i in items if i["end"] - i["start"] < MIN_LONG_FRAMES)
    if short_clips and progress:
        progress(f"{short_clips} clip(s) under {MIN_LONG_FRAMES} frames - too short to measure alone"
                 + (f"; measuring {len(stretches)} stretch(es) of consecutive clips instead"
                    if stretches else ""))
    jobs = items + stretches
    entered = False
    reused = 0
    try:
        for index, item in enumerate(jobs, 1):
            length = item["end"] - item["start"]
            if length < MIN_LONG_FRAMES:
                within = next((s["label"] for s in stretches
                               if s["start"] <= item["start"] < s["end"]), "")
                samples.append(RenderSample(
                    item=item["name"], track=item["track"], start=item["start"], end=item["end"],
                    sample_start=item["start"], frames=0, render_ms=0.0, status=TOO_SHORT,
                    fusion_tools=item["fusion_tools"], color_nodes=item["color_nodes"],
                    label=_label(item, fps), in_stretch=within))
                continue
            key = _render_key(item, timeline_fp, seconds, short_seconds)
            cached = cache.get(key)
            if cached is not None:
                reused += 1
                samples.append(cached)
                continue
            if not entered:
                queue.__enter__()
                entered = True
            n, n_short = sample_sizes(length, fps, seconds, short_seconds)
            short_ms, ms, status = 0.0, 0.0, "Failed"
            try:
                if n_short:
                    short_start = item["start"] + max(0, (length - n_short) // 2)
                    if progress:
                        progress(f"sample {index}/{len(jobs)}: {item['name']} - short ({n_short} frames)")
                    short_ms, short_status = queue.render_range(short_start, short_start + n_short - 1)
                    if short_status != "Complete":
                        n_short, short_ms = 0, 0.0
                    elif short_ms > 0:
                        # Worst case the whole short time was per-frame work: keep the long
                        # sample inside the budget, but always well above the short one.
                        affordable = int(budget_s * 1000.0 * n_short / short_ms)
                        n = max(min(n, affordable), 4 * n_short)
                        n = min(n, length)
                sample_start = item["start"] + max(0, (length - n) // 2)
                if progress:
                    progress(f"sample {index}/{len(jobs)}: {item['name']} - long ({n} frames)")
                ms, status = queue.render_range(sample_start, sample_start + n - 1)
            except RuntimeError as exc:
                ms, status, n_short, short_ms = 0.0, f"Failed: {exc}", 0, 0.0
                sample_start = item["start"]
            if n_short >= n:
                n_short, short_ms = 0, 0.0
            sample = RenderSample(
                item=item["name"], track=item["track"], start=item["start"], end=item["end"],
                sample_start=sample_start, frames=n, render_ms=ms, status=status,
                fusion_tools=item["fusion_tools"], color_nodes=item["color_nodes"],
                short_frames=n_short, short_ms=short_ms, label=_label(item, fps),
                measured_at=time.time(), clips=list(item.get("clips", [])),
            )
            cache.put(key, sample)
            samples.append(sample)
    finally:
        if entered:
            queue.__exit__(None, None, None)
    if reused and progress:
        progress(f"render: {reused} sample(s) from cache")

    profile = RenderProfile(str(timeline.GetName()), fps, queue.format, queue.codec, samples,
                            render_cache=render_cache)
    estimate_export(profile)
    return profile


def estimate_export(profile: RenderProfile) -> None:
    """Whole-timeline export time, charging each frame to the top-most clip covering it.

    A sample rendered on V2 already includes whatever is under it on V1, so
    frames are assigned to the highest track that covers them and counted once.
    Stretches (track 0) take what no measured clip covers; clips too short to
    measure cover nothing.
    """
    covered: list[tuple[int, int]] = []
    total_ms = 0.0
    per_item: dict[str, float] = {}
    total_frames = 0
    for s in sorted(profile.samples, key=lambda s: -s.track):
        if s.too_short:
            continue
        uncovered = _subtract((s.start, s.end), covered)
        frames = sum(b - a for a, b in uncovered)
        covered = _merge(covered + uncovered)
        if not s.ok:
            continue
        cost = frames * s.ms_per_frame
        per_item[s.label] = per_item.get(s.label, 0.0) + cost
        total_ms += cost
        total_frames += frames
    profile.total_frames = total_frames
    profile.estimated_export_s = round(total_ms / 1000.0, 1)
    profile.shares = {k: (v / total_ms if total_ms else 0.0) for k, v in per_item.items()}


def _subtract(span: tuple[int, int], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pieces = [span]
    for a, b in covered:
        next_pieces = []
        for x, y in pieces:
            if b <= x or a >= y:
                next_pieces.append((x, y))
                continue
            if x < a:
                next_pieces.append((x, a))
            if b < y:
                next_pieces.append((b, y))
        pieces = next_pieces
    return [(x, y) for x, y in pieces if y > x]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# -------------------------------------------------------------- findings
GUESSED_FX = {"fusion-comp", "deep-grade"}     # the scan's rules of thumb about effects


def apply_render_measurements(report: ScanReport, profile: RenderProfile) -> None:
    """Let the measurement override the scan's guesses about effects.

    The scan flags every Fusion comp and deep grade as probably expensive. Once
    the item has actually been rendered - on its own or inside a stretch - that
    guess is replaced: at or above real time it becomes an info note saying so;
    below it, an item's own ``render-*`` finding already says what is wrong,
    and inside a heavy stretch the guess stays, since it is the best pointer to
    which cut carries the cost.
    """
    own = {s.label: s for s in profile.samples if s.ok and not s.stretch}
    within = {label: s for s in profile.samples if s.ok and s.stretch for label in s.clips}
    kept: list[Finding] = []
    noted: set[str] = set()
    for f in report.findings:
        if f.code not in GUESSED_FX:
            kept.append(f)
            continue
        sample = own.get(f.subject) or within.get(f.subject)
        if sample is None:
            kept.append(f)
            continue
        ratio = profile.ratio(sample)
        if ratio < 1.0:
            if sample.stretch:
                kept.append(f)
            continue
        if f.subject in noted:
            continue
        noted.add(f.subject)
        where = " (in a stretch of short clips)" if sample.stretch else ""
        if profile.from_cache_mode:
            kept.append(Finding(
                "info", "fx-cached-ok", f.subject,
                f"plays from the Render Cache at {ratio:.2f}x real time{where}",
                f"Render Cache is {profile.render_cache}, so Resolve rendered this from the cache; "
                "fine to play once the cache is built. The raw cost of the effects was not measured."))
        else:
            kept.append(Finding(
                "info", "fx-measured-ok", f.subject,
                f"effects measured fine: renders at {ratio:.2f}x real time{where}",
                "The scan flagged the effects on this clip as probably expensive; Resolve's own "
                "render queue says otherwise on this machine, so they are not a bottleneck here."))
    report.findings = sort_findings(kept)


def render_findings(profile: RenderProfile) -> list[Finding]:
    out: list[Finding] = []
    good = [s for s in profile.samples if s.ok]
    for s in profile.samples:
        if s.too_short:
            continue
        if not s.ok:
            out.append(Finding("medium", "render-sample-failed", s.label,
                               f"sample render did not complete ({s.status})",
                               "Resolve could not render this range through the queue; check the "
                               "clip plays at all and that the render queue is idle."))
            continue
        ratio = profile.ratio(s)
        carries = []
        if s.fusion_tools:
            carries.append("Fusion tools " + ", ".join(sorted(set(s.fusion_tools))[:4]))
        if s.color_nodes > 1:
            carries.append(f"a {s.color_nodes}-node grade")
        what = ("; it carries " + " and ".join(carries)) if carries else ""
        rate = f"{ratio:.2f}x real time ({s.ms_per_frame:.0f} ms/frame)"
        if s.stretch:
            rate += f" across {len(s.clips)} short clips"
            what += (". A stretch's number is the average over the clips it spans; the heavy "
                     "one is whichever carries the effects")
        if ratio < 0.5:
            out.append(Finding("high", "render-heavy", s.label,
                               f"renders at {rate}",
                               "Measured by Resolve's own render queue on this machine, so this "
                               "includes everything: decode, grade, Fusion and scaling" + what +
                               ". Render Cache or render-in-place for this clip is the fix."))
        elif ratio < 1.0:
            out.append(Finding("medium", "render-slow", s.label,
                               f"renders at {rate}",
                               "Includes the sample encode, so playback will be somewhat better "
                               "than this - but any added effect tips it over" + what + "."))
        share = profile.shares.get(s.label, 0.0)
        if share >= 0.5 and len(good) > 1:
            out.append(Finding("medium", "export-dominant", s.label,
                               f"accounts for {share:.0%} of the estimated export time",
                               "Whatever this clip carries is where export time goes. Simplify "
                               "or pre-render it and the whole export speeds up."))
    return sort_findings(out)
