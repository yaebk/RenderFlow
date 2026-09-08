"""A FrameForge host that caches DaVinci Resolve segments by pre-rendering them.

WHY IT WORKS THIS WAY
---------------------
Resolve's scripting API exposes no way to control its render cache. Searching
Blackmagic's own API reference for "cache" turns up exactly one hit -
``isArchiveRenderCache``, a project-archiving flag. There is no trigger, no
mode, no state query. So this adapter does not try to drive Resolve's cache.

Instead it builds a cache Resolve *will* use, out of APIs that are documented
and supported:

    1. SetRenderSettings({MarkIn, MarkOut, ...})  - restrict to one segment
    2. AddRenderJob() / StartRendering(jobId)     - render just that range
    3. mediaPool.ImportMedia([path])              - bring the result back in
    4. mediaPool.AppendToTimeline([{...}])        - drop it on a cache track
                                                    at recordFrame

A baked clip on the top track plays back with no effect processing, so the
segment becomes free. This is what "render in place" does by hand; FrameForge
automates it and, crucially, chooses the *order* - expensive work near the
playhead first, instead of a front-to-back sweep.

WHAT IT COSTS YOU
-----------------
It writes to your timeline. Baked segments land on a dedicated video track
named "FrameForge Cache", which is excluded from scheduling and can be removed
wholesale with :meth:`ResolveHost.clear_cache_track`. Work on a duplicate
timeline.

STATUS
------
Every API call here is verified present in Blackmagic's bundled scripting
README. None of it has been executed against a live Resolve. Treat the first
run as an experiment, and use ``dry_run=True`` to see the plan first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from frameforge.adapters.resolve.reader import (
    CACHE_TRACK_NAME,
    read_timeline,
    timecode_to_frames,
    timeline_fps,
)
from frameforge.host import Segment

TERMINAL_STATUSES = {"Complete", "Failed", "Cancelled"}


@dataclass
class Baked:
    """One segment we rendered and placed on the cache track."""

    segment_name: str
    path: Path
    record_frame: int
    timeline_item: object | None = None


@dataclass
class ResolveHost:
    """Implements the FrameForge host protocol against a running Resolve."""

    resolve: object
    cache_dir: Path
    render_format: str = "mp4"
    render_codec: str = "H264"
    poll_interval: float = 0.25
    start_grace_s: float = 5.0
    dry_run: bool = False

    _fps: float = field(default=24.0, init=False)
    baked: dict[str, Baked] = field(default_factory=dict, init=False)
    log: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._fps = timeline_fps(self.timeline, self.project)

    # ------------------------------------------------------------ handles
    @property
    def project(self):
        project = self.resolve.GetProjectManager().GetCurrentProject()
        if project is None:
            raise RuntimeError("No project is open in Resolve.")
        return project

    @property
    def timeline(self):
        timeline = self.project.GetCurrentTimeline()
        if timeline is None:
            raise RuntimeError("No timeline is open in Resolve.")
        return timeline

    def read_timeline(self):
        """The current timeline as a FrameForge Timeline (cache track excluded)."""
        return read_timeline(self.timeline, self.project)

    def _note(self, message: str) -> None:
        self.log.append(message)

    def _find_cache_track(self) -> int | None:
        timeline = self.timeline
        for index in range(1, int(timeline.GetTrackCount("video")) + 1):
            if timeline.GetTrackName("video", index) == CACHE_TRACK_NAME:
                return index
        return None

    # --------------------------------------------------- cache track setup
    def ensure_cache_track(self) -> int:
        """Index of the cache track, creating it if it isn't there yet."""
        existing = self._find_cache_track()
        if existing is not None:
            return existing
        timeline = self.timeline
        if self.dry_run:
            return int(timeline.GetTrackCount("video")) + 1
        if not timeline.AddTrack("video"):
            raise RuntimeError("AddTrack('video') failed - cannot create a cache track")
        index = int(timeline.GetTrackCount("video"))
        timeline.SetTrackName("video", index, CACHE_TRACK_NAME)
        self._note(f"created cache track V{index} ({CACHE_TRACK_NAME})")
        return index

    def adopt_existing(self) -> int:
        """Register baked clips left on the cache track by a previous run."""
        index = self._find_cache_track()
        if index is None:
            return 0
        found = 0
        for item in self.timeline.GetItemListInTrack("video", index) or []:
            try:
                start, end = int(item.GetStart()), int(item.GetEnd())
            except (AttributeError, TypeError, ValueError):
                continue
            name = f"{start}-{end}"
            self.baked[name] = Baked(name, Path(""), start, item)
            found += 1
        if found:
            self._note(f"adopted {found} baked segment(s) already on the cache track")
        return found

    # --------------------------------------------------- required: render
    def render(self, segment: Segment) -> None:
        """Render one segment and place it on the cache track."""
        if self.dry_run:
            self._note(
                f"[dry-run] would bake {segment.name} "
                f"frames {segment.start}-{segment.end}"
            )
            self.baked[segment.name] = Baked(segment.name, Path(""), segment.start)
            return

        track_index = self.ensure_cache_track()
        path = self._render_range(segment)
        media_item = self._import(path)
        timeline_item = self._place(media_item, segment, track_index)
        self.baked[segment.name] = Baked(segment.name, path, segment.start, timeline_item)
        self._note(f"baked {segment.name} -> {path.name} on V{track_index}")

    def _render_range(self, segment: Segment) -> Path:
        project = self.project
        try:
            project.SetCurrentRenderFormatAndCodec(self.render_format, self.render_codec)
        except AttributeError:
            pass

        stem = f"ff_{segment.start}_{segment.end}"
        settings = {
            "SelectAllFrames": False,
            "MarkIn": int(segment.start),
            "MarkOut": int(segment.end) - 1,   # MarkOut is inclusive
            "TargetDir": str(self.cache_dir),
            "CustomName": stem,
            "ExportVideo": True,
            "ExportAudio": False,
        }
        if not project.SetRenderSettings(settings):
            raise RuntimeError(f"SetRenderSettings failed for {segment.name}")

        job_id = project.AddRenderJob()
        if not job_id:
            raise RuntimeError(f"AddRenderJob failed for {segment.name}")

        try:
            project.StartRendering(job_id)
            # StartRendering is asynchronous and takes a moment to spin up.
            grace = time.perf_counter() + self.start_grace_s
            while time.perf_counter() < grace and not project.IsRenderingInProgress():
                status = (project.GetRenderJobStatus(job_id) or {}).get("JobStatus")
                if status in TERMINAL_STATUSES:
                    break
                time.sleep(0.05)
            while project.IsRenderingInProgress():
                time.sleep(self.poll_interval)

            status = (project.GetRenderJobStatus(job_id) or {}).get("JobStatus", "Unknown")
            if status not in ("Complete", "Unknown"):
                raise RuntimeError(f"render of {segment.name} ended as {status}")
        finally:
            try:
                project.DeleteRenderJob(job_id)
            except AttributeError:
                pass

        # Resolve decides the final extension, so find what it actually wrote.
        matches = sorted(self.cache_dir.glob(stem + ".*"))
        if not matches:
            matches = sorted(self.cache_dir.glob(stem + "*"))
        if not matches:
            raise RuntimeError(
                f"render reported success but no file matching {stem}.* appeared "
                f"in {self.cache_dir}"
            )
        return matches[0]

    def _import(self, path: Path):
        media_pool = self.project.GetMediaPool()
        items = media_pool.ImportMedia([str(path)])
        if not items:
            raise RuntimeError(f"ImportMedia failed for {path}")
        return items[0]

    def _place(self, media_item, segment: Segment, track_index: int):
        media_pool = self.project.GetMediaPool()
        clip_info = {
            "mediaPoolItem": media_item,
            "startFrame": 0,
            "endFrame": max(0, segment.length - 1),
            "trackIndex": track_index,
            "recordFrame": int(segment.start),
        }
        placed = media_pool.AppendToTimeline([clip_info])
        if not placed:
            raise RuntimeError(
                f"AppendToTimeline failed for {segment.name} at frame {segment.start}"
            )
        return placed[0]

    # --------------------------------------------------- optional protocol
    def playhead(self) -> float | None:
        try:
            return float(
                timecode_to_frames(self.timeline.GetCurrentTimecode(), self._fps)
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def is_cached(self, segment: Segment) -> bool:
        return segment.name in self.baked

    def evict(self, segment: Segment) -> None:
        baked = self.baked.pop(segment.name, None)
        if baked is None or self.dry_run:
            return
        if baked.timeline_item is not None:
            try:
                self.timeline.DeleteClips([baked.timeline_item], False)
            except (AttributeError, TypeError):
                pass
        try:
            if baked.path and baked.path.exists():
                baked.path.unlink()
        except OSError:
            pass
        self._note(f"evicted {segment.name}")

    # ------------------------------------------------------------ cleanup
    def clear_cache_track(self) -> int:
        """Remove every baked clip and the cache track itself. Undoes everything."""
        index = self._find_cache_track()
        if index is None:
            return 0
        timeline = self.timeline
        items = list(timeline.GetItemListInTrack("video", index) or [])
        if items:
            timeline.DeleteClips(items, False)
        timeline.DeleteTrack("video", index)
        for baked in self.baked.values():
            try:
                if baked.path and baked.path.exists():
                    baked.path.unlink()
            except OSError:
                pass
        self.baked.clear()
        self._note(f"cleared cache track V{index} ({len(items)} clips)")
        return len(items)
