"""Resolve adapter, exercised against a fake Resolve application.

None of this can be run against a real Resolve here, so the fake mirrors the
documented API surface exactly - every method it implements is one that appears
in Blackmagic's bundled scripting README, with the same signature.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CacheEngine, Segment, capabilities_of
from frameforge.adapters.resolve import CACHE_TRACK_NAME, ResolveHost, read_timeline
from frameforge.adapters.resolve.reader import (
    detect_effects,
    frames_to_timecode,
    timecode_to_frames,
)


# --------------------------------------------------------------- fake Resolve
class FakeGraph:
    def __init__(self, nodes):
        self._nodes = nodes            # list[list[str]] - tools per node

    def GetNumNodes(self):
        return len(self._nodes)

    def GetToolsInNode(self, index):
        return list(self._nodes[index - 1])


class FakeItem:
    def __init__(self, name, start, end, fusion=0, nodes=()):
        self._name, self._start, self._end = name, start, end
        self._fusion = fusion
        self._graph = FakeGraph(list(nodes)) if nodes else FakeGraph([])

    def GetName(self):
        return self._name

    def GetStart(self):
        return self._start

    def GetEnd(self):
        return self._end

    def GetFusionCompCount(self):
        return self._fusion

    def GetNodeGraph(self, layer=1):
        return self._graph


class FakeMediaPool:
    def __init__(self, timeline):
        self.timeline = timeline
        self.imported = []
        self.appended = []

    def ImportMedia(self, paths):
        self.imported.extend(paths)
        return [f"mpi:{Path(paths[0]).name}"]

    def AppendToTimeline(self, clip_infos):
        placed = []
        for info in clip_infos:
            self.appended.append(dict(info))
            item = FakeItem(
                str(info["mediaPoolItem"]),
                info["recordFrame"],
                info["recordFrame"] + info["endFrame"] + 1,
            )
            self.timeline.tracks.setdefault(info["trackIndex"], []).append(item)
            placed.append(item)
        return placed


class FakeTimeline:
    def __init__(self, tracks, fps="24.0", timecode="01:00:00:00"):
        self.tracks = dict(tracks)                 # index -> [FakeItem]
        self.names = {i: f"V{i}" for i in self.tracks}
        self.fps = fps
        self.timecode = timecode
        self.deleted = []

    def GetName(self):
        return "Fake Timeline"

    def GetSetting(self, key):
        return self.fps if key == "timelineFrameRate" else None

    def GetTrackCount(self, kind):
        return max(self.tracks) if self.tracks else 0

    def GetTrackName(self, kind, index):
        return self.names.get(index, "")

    def SetTrackName(self, kind, index, name):
        self.names[index] = name
        return True

    def AddTrack(self, kind, options=None):
        index = (max(self.tracks) if self.tracks else 0) + 1
        self.tracks[index] = []
        self.names[index] = f"V{index}"
        return True

    def DeleteTrack(self, kind, index):
        self.tracks.pop(index, None)
        self.names.pop(index, None)
        return True

    def GetItemListInTrack(self, kind, index):
        return list(self.tracks.get(index, []))

    def DeleteClips(self, items, ripple=False):
        self.deleted.extend(items)
        for track in self.tracks.values():
            for item in items:
                if item in track:
                    track.remove(item)
        return True

    def GetCurrentTimecode(self):
        return self.timecode


class FakeProject:
    def __init__(self, timeline, fail_at=None):
        self.timeline = timeline
        self.media_pool = FakeMediaPool(timeline)
        self.fail_at = fail_at             # "settings" | "addjob" | "status"
        self.jobs = {}
        self.deleted_jobs = []
        self.settings_seen = []
        self.format_codec = None
        self._counter = 0

    def GetName(self):
        return "Fake Project"

    def GetSetting(self, key):
        return None

    def GetCurrentTimeline(self):
        return self.timeline

    def GetMediaPool(self):
        return self.media_pool

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        self.format_codec = (fmt, codec)
        return True

    def SetRenderSettings(self, settings):
        if self.fail_at == "settings":
            return False
        self.settings_seen.append(dict(settings))
        return True

    def AddRenderJob(self):
        if self.fail_at == "addjob":
            return None
        self._counter += 1
        job = f"job{self._counter}"
        self.jobs[job] = self.settings_seen[-1]
        return job

    def StartRendering(self, job_id, *rest, **kw):
        settings = self.jobs[job_id]
        # A real render writes a file; so does the fake, so globbing works.
        out = Path(settings["TargetDir"]) / (settings["CustomName"] + ".mp4")
        out.write_bytes(b"fake")
        return True

    def IsRenderingInProgress(self):
        return False

    def GetRenderJobStatus(self, job_id):
        if self.fail_at == "status":
            return {"JobStatus": "Failed"}
        return {"JobStatus": "Complete", "CompletionPercentage": 100}

    def DeleteRenderJob(self, job_id):
        self.deleted_jobs.append(job_id)
        self.jobs.pop(job_id, None)
        return True


class FakeResolve:
    def __init__(self, project):
        self._project = project

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self._project


def make_resolve(tracks=None, **kw):
    tracks = tracks or {
        1: [FakeItem("wide", 0, 240), FakeItem("close", 240, 480, nodes=[["Gaussian Blur"]])],
        2: [FakeItem("grade", 100, 300, nodes=[["Lumetri"], ["Temporal NR"]])],
    }
    timeline = FakeTimeline(tracks)
    return FakeResolve(FakeProject(timeline, **kw)), timeline


# ------------------------------------------------------------------ timecode
@pytest.mark.parametrize("tc,fps,frames", [
    ("01:00:00:00", 24, 86400),
    ("00:00:10:00", 24, 240),
    ("00:00:01:12", 24, 36),
    ("00:00:01;12", 30, 42),
])
def test_timecode_to_frames(tc, fps, frames):
    assert timecode_to_frames(tc, fps) == frames


def test_timecode_round_trip():
    for frame in (0, 1, 239, 86400, 108202):
        assert timecode_to_frames(frames_to_timecode(frame, 24), 24) == frame


# -------------------------------------------------------------------- reader
def test_reads_all_video_tracks():
    _, timeline = make_resolve()
    tl = read_timeline(timeline)
    assert len(tl) == 3
    assert {c.track for c in tl} == {1, 2}
    assert tl.fps == 24.0


def test_effects_come_from_the_colour_node_graph():
    """This is information no EDL or FCP XML carries."""
    _, timeline = make_resolve()
    tl = read_timeline(timeline)
    grade = next(c for c in tl if c.name == "grade")
    # "Lumetri" -> Color Correction, "Temporal NR" -> Temporal Noise Reduction
    assert sorted(grade.effects) == ["Color Correction", "Temporal Noise Reduction"]


def test_fusion_comp_counts_as_composite():
    item = FakeItem("fx", 0, 100, fusion=2)
    assert "Composite" in detect_effects(item)


def test_deep_grade_costs_something_even_when_tools_are_unnamed():
    item = FakeItem("graded", 0, 100, nodes=[[], [], []])
    assert detect_effects(item) == ["Color Correction", "Color Correction"]


def test_cache_track_is_excluded_from_scheduling():
    """Otherwise FrameForge would schedule caching of its own cache."""
    tracks = {
        1: [FakeItem("wide", 0, 240)],
        2: [FakeItem("baked", 0, 240)],
    }
    timeline = FakeTimeline(tracks)
    timeline.SetTrackName("video", 2, CACHE_TRACK_NAME)
    tl = read_timeline(timeline)
    assert [c.name for c in tl] == ["wide"]


# ---------------------------------------------------------------------- host
def test_host_advertises_the_right_capabilities():
    caps = sorted(str(c) for c in capabilities_of(ResolveHost.__new__(ResolveHost)))
    assert caps == ["evict", "is_cached", "playhead", "render"]


def test_render_runs_the_full_four_step_sequence(tmp_path):
    resolve, timeline = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path)
    project = resolve.GetCurrentProject()

    host.render(Segment("close", 240, 480, cost=9.0))

    # 1. render settings restricted to the segment
    settings = project.settings_seen[-1]
    assert settings["SelectAllFrames"] is False
    assert settings["MarkIn"] == 240
    assert settings["MarkOut"] == 479          # inclusive, so end - 1
    # 2. job created, started and cleaned up
    assert project.deleted_jobs == ["job1"]
    # 3. result imported
    assert len(project.media_pool.imported) == 1
    # 4. placed at the right frame on the cache track
    placed = project.media_pool.appended[-1]
    assert placed["recordFrame"] == 240
    assert placed["endFrame"] == 239
    assert timeline.GetTrackName("video", placed["trackIndex"]) == CACHE_TRACK_NAME


def test_cache_track_is_created_once_and_reused(tmp_path):
    resolve, timeline = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path)
    before = timeline.GetTrackCount("video")
    host.render(Segment("a", 0, 100))
    host.render(Segment("b", 100, 200))
    assert timeline.GetTrackCount("video") == before + 1
    tracks = {info["trackIndex"] for info in resolve.GetCurrentProject().media_pool.appended}
    assert len(tracks) == 1


def test_is_cached_tracks_what_was_baked(tmp_path):
    resolve, _ = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path)
    segment = Segment("a", 0, 100)
    assert host.is_cached(segment) is False
    host.render(segment)
    assert host.is_cached(segment) is True


def test_evict_removes_the_clip_and_the_file(tmp_path):
    resolve, timeline = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path)
    segment = Segment("a", 0, 100)
    host.render(segment)
    path = host.baked["a"].path
    assert path.exists()

    host.evict(segment)
    assert not host.is_cached(segment)
    assert not path.exists()
    assert timeline.deleted


def test_playhead_reads_the_timecode(tmp_path):
    resolve, timeline = make_resolve()
    timeline.timecode = "01:00:10:00"
    host = ResolveHost(resolve, cache_dir=tmp_path)
    assert host.playhead() == 86640.0


def test_adopt_existing_finds_previous_bakes(tmp_path):
    tracks = {1: [FakeItem("wide", 0, 240)], 2: [FakeItem("ff", 0, 240)]}
    timeline = FakeTimeline(tracks)
    timeline.SetTrackName("video", 2, CACHE_TRACK_NAME)
    host = ResolveHost(FakeResolve(FakeProject(timeline)), cache_dir=tmp_path)
    assert host.adopt_existing() == 1
    assert host.is_cached(Segment("0-240", 0, 240))


def test_clear_cache_track_undoes_everything(tmp_path):
    resolve, timeline = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path)
    host.render(Segment("a", 0, 100))
    host.render(Segment("b", 100, 200))
    tracks_before = timeline.GetTrackCount("video")

    removed = host.clear_cache_track()
    assert removed == 2
    assert timeline.GetTrackCount("video") == tracks_before - 1
    assert host.baked == {}
    assert not list(tmp_path.glob("ff_*"))


def test_dry_run_touches_nothing(tmp_path):
    resolve, timeline = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path, dry_run=True)
    tracks_before = timeline.GetTrackCount("video")
    host.render(Segment("a", 0, 100))
    assert timeline.GetTrackCount("video") == tracks_before
    assert resolve.GetCurrentProject().media_pool.appended == []
    assert not list(tmp_path.glob("*"))
    assert "would bake" in host.log[-1]


# ----------------------------------------------------------------- failures
@pytest.mark.parametrize("fail_at,message", [
    ("settings", "SetRenderSettings failed"),
    ("addjob", "AddRenderJob failed"),
    ("status", "ended as Failed"),
])
def test_render_failures_raise_with_a_clear_reason(tmp_path, fail_at, message):
    resolve, _ = make_resolve(fail_at=fail_at)
    host = ResolveHost(resolve, cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match=message):
        host.render(Segment("a", 0, 100))


def test_missing_output_file_is_reported(tmp_path):
    resolve, _ = make_resolve()
    project = resolve.GetCurrentProject()
    project.StartRendering = lambda *a, **k: True     # renders nothing
    host = ResolveHost(resolve, cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="no file matching"):
        host.render(Segment("a", 0, 100))


# --------------------------------------------------------------- end to end
def test_engine_drives_the_host_in_priority_order(tmp_path):
    tracks = {
        1: [
            FakeItem("cheap", 0, 240),
            FakeItem("mid", 240, 480, nodes=[["Gaussian Blur"]]),
            FakeItem("dear", 480, 720, nodes=[["Temporal NR"], ["Optical Flow"]]),
        ],
    }
    resolve, timeline = make_resolve(tracks=tracks)
    host = ResolveHost(resolve, cache_dir=tmp_path)

    engine = CacheEngine(host, host.read_timeline())
    timeline.timecode = frames_to_timecode(600, 24)   # parked on the expensive clip
    engine.run(until_complete=True)

    baked_order = [line.split()[1] for line in host.log if line.startswith("baked")]
    assert baked_order[0] == "dear"
    assert set(baked_order) == {"cheap", "mid", "dear"}


def test_engine_skips_segments_the_host_already_has(tmp_path):
    resolve, timeline = make_resolve()
    host = ResolveHost(resolve, cache_dir=tmp_path)
    engine = CacheEngine(host, host.read_timeline())
    engine.run(until_complete=True)
    first_pass = len([l for l in host.log if l.startswith("baked")])

    engine2 = CacheEngine(host, host.read_timeline())
    engine2.run(until_complete=True)
    assert len([l for l in host.log if l.startswith("baked")]) == first_pass
