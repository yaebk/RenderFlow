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
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from renderflow.scan import Finding, _fusion_tools, _int, _node_count, item_label

DEFAULT_SECONDS = 10.0          # long sample, in timeline seconds
DEFAULT_SHORT_SECONDS = 2.0     # short sample; slope between the two cancels job set-up
DEFAULT_BUDGET_S = 60.0         # cap on wall time per long sample
MIN_LONG_FRAMES = 120           # below this the pipeline's parallelism hides the slope
SAMPLE_NAME = "renderflow_sample"
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

    def __post_init__(self) -> None:
        if not self.label:
            self.label = f"{self.item} @V{self.track} #{self.start}"

    @property
    def ok(self) -> bool:
        return self.status == "Complete" and self.frames > 0 and self.render_ms > 0

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
    shares: dict[str, float] = field(default_factory=dict)   # item -> share of export time
    total_frames: int = 0

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
        return data

    def text(self) -> str:
        overheads = [s.overhead_ms for s in self.samples if s.two_point]
        note = (f", per-job overhead ~{sum(overheads) / len(overheads):.0f} ms removed"
                if overheads else "")
        lines = [f"timeline : {self.timeline} @ {self.fps:g} fps, sample codec "
                 f"{self.format}/{self.codec}{note}",
                 f"{'clip':<34} {'trk':>3} {'frames':>7} {'ms/frame':>9} {'render':>8} "
                 f"{'ratio':>6} {'export':>7}  carries"]
        for s in self.samples:
            name = (s.item[:21] + "...") if len(s.item) > 24 else s.item
            name = f"{name} @V{s.track} {s.label.rsplit(' ', 1)[-1]}"
            carries = []
            if s.fusion_tools:
                carries.append("fusion " + ",".join(sorted(set(s.fusion_tools))[:3]))
            if s.color_nodes > 1:
                carries.append(f"{s.color_nodes} nodes")
            if not s.ok:
                lines.append(f"{name:<34} {s.track:>3} {s.length:>7} {'-':>9} {'-':>8} {'-':>6} "
                             f"{'-':>7}  {s.status}")
                continue
            share = self.shares.get(s.label, 0.0)
            lines.append(f"{name:<34} {s.track:>3} {s.length:>7} {s.ms_per_frame:>9.1f} "
                         f"{s.render_fps:>7.1f}  {self.ratio(s):>5.2f}x {share:>6.0%}  "
                         f"{', '.join(carries)}")
        if self.estimated_export_s:
            minutes, seconds = divmod(int(round(self.estimated_export_s)), 60)
            lines.append("")
            lines.append(f"estimated export of the whole timeline at {self.codec}: "
                         f"{minutes}m {seconds:02d}s for {self.total_frames} frames "
                         f"({self.total_frames / self.fps:.0f}s of video)")
        return "\n".join(lines)


# ------------------------------------------------------------ the queue
class RenderQueue:
    """One-sample renders through Resolve's queue, with everything put back after."""

    def __init__(self, resolve, target_dir: str | None = None, fmt: str | None = None,
                 codec: str | None = None, poll_s: float = 0.05, timeout_s: float = JOB_TIMEOUT_S):
        self.resolve = resolve
        self.project = resolve.GetProjectManager().GetCurrentProject()
        self.timeline = self.project.GetCurrentTimeline()
        self.target_dir = target_dir or tempfile.mkdtemp(prefix="renderflow_")
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
        os.makedirs(self.target_dir, exist_ok=True)
        if not self.project.SetCurrentRenderFormatAndCodec(self.format, self.codec):
            raise RuntimeError(f"Resolve refused render format {self.format}/{self.codec}")
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
        if self._own_dir:
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
            except Exception:                                       # noqa: BLE001
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
        except Exception:                                           # noqa: BLE001
            continue
        if codec in codecs.values():
            return fmt, codec
    current = project.GetCurrentRenderFormatAndCodec() or {}
    if current.get("format") and current.get("codec"):
        return current["format"], current["codec"]
    raise RuntimeError("no usable render format found")


# ------------------------------------------------------------- measuring
def timeline_items(timeline) -> list[dict[str, Any]]:
    items = []
    for track in range(1, _int(timeline.GetTrackCount("video")) + 1):
        for item in timeline.GetItemListInTrack("video", track) or []:
            start, end = _int(item.GetStart()), _int(item.GetEnd())
            if end <= start:
                continue
            items.append({
                "name": str(item.GetName()), "track": track, "start": start, "end": end,
                "fusion_tools": _fusion_tools(item), "color_nodes": _node_count(item),
            })
    return items


def _label(item: dict, fps: float) -> str:
    return item_label(item["name"], item["track"], item["start"], fps)


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
                queue: RenderQueue | None = None) -> RenderProfile:
    """Sample every clip on the current timeline and return a :class:`RenderProfile`.

    Each clip gets a short sample first, then a long one whose length is cut
    back if the short one predicts it would take more than ``budget_s`` of
    wall time (a heavy Fusion clip at seconds per frame). The slope between
    the two cancels the fixed per-job cost.
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

    samples: list[RenderSample] = []
    with queue:
        for index, item in enumerate(items, 1):
            length = item["end"] - item["start"]
            n, n_short = sample_sizes(length, fps, seconds, short_seconds)
            short_ms, ms, status = 0.0, 0.0, "Failed"
            try:
                if n_short:
                    short_start = item["start"] + max(0, (length - n_short) // 2)
                    if progress:
                        progress(f"sample {index}/{len(items)}: {item['name']} - short ({n_short} frames)")
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
                    progress(f"sample {index}/{len(items)}: {item['name']} - long ({n} frames)")
                ms, status = queue.render_range(sample_start, sample_start + n - 1)
            except RuntimeError as exc:
                ms, status, n_short, short_ms = 0.0, f"Failed: {exc}", 0, 0.0
                sample_start = item["start"]
            if n_short >= n:
                n_short, short_ms = 0, 0.0
            samples.append(RenderSample(
                item=item["name"], track=item["track"], start=item["start"], end=item["end"],
                sample_start=sample_start, frames=n, render_ms=ms, status=status,
                fusion_tools=item["fusion_tools"], color_nodes=item["color_nodes"],
                short_frames=n_short, short_ms=short_ms, label=_label(item, fps),
            ))

    profile = RenderProfile(str(timeline.GetName()), fps, queue.format, queue.codec, samples)
    estimate_export(profile)
    return profile


def estimate_export(profile: RenderProfile) -> None:
    """Whole-timeline export time, charging each frame to the top-most clip covering it.

    A sample rendered on V2 already includes whatever is under it on V1, so
    frames are assigned to the highest track that covers them and counted once.
    """
    covered: list[tuple[int, int]] = []
    total_ms = 0.0
    per_item: dict[str, float] = {}
    total_frames = 0
    for s in sorted(profile.samples, key=lambda s: -s.track):
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
def render_findings(profile: RenderProfile) -> list[Finding]:
    out: list[Finding] = []
    good = [s for s in profile.samples if s.ok]
    cheapest = min((s.ms_per_frame for s in good), default=0.0)
    for s in profile.samples:
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
        relative = (f" - {s.ms_per_frame / cheapest:.1f}x the cheapest clip on this timeline"
                    if cheapest and len(good) > 1 else "")
        rate = f"{s.ms_per_frame:.0f} ms/frame, {ratio:.2f}x real time{relative}"
        if ratio < 0.5:
            out.append(Finding("high", "render-heavy", s.label,
                               f"renders far below real time: {rate}",
                               "Measured by Resolve's own render queue on this machine, so this "
                               "includes everything: decode, grade, Fusion and scaling" + what +
                               ". Render Cache or render-in-place for this clip is the fix."))
        elif ratio < 1.0:
            out.append(Finding("medium", "render-slow", s.label,
                               f"renders below real time: {rate}",
                               "Includes the sample encode, so playback will be somewhat better "
                               "than this - but any added effect tips it over" + what + "."))
        share = profile.shares.get(s.label, 0.0)
        if share >= 0.5 and len(good) > 1:
            out.append(Finding("medium", "export-dominant", s.label,
                               f"accounts for {share:.0%} of the estimated export time",
                               "Whatever this clip carries is where export time goes. Simplify "
                               "or pre-render it and the whole export speeds up."))
    out.sort(key=lambda f: ({"high": 0, "medium": 1, "info": 2}[f.severity], f.subject))
    return out
