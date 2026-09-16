"""Render-cost tests against a fake Resolve whose render queue behaves like the
real one did in a live probe: MarkOut inclusive, TimeTakenToRenderInMs in the
job status, page switch to deliver and playhead move as side effects."""

import os

import pytest

from renderflow import rendercost
from renderflow.rendercost import (
    RenderCache,
    RenderProfile,
    RenderQueue,
    RenderSample,
    _merge,
    _subtract,
    choose_format,
    estimate_export,
    plan_stretches,
    render_cost,
    render_findings,
    sample_sizes,
)


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(rendercost, "RENDER_CACHE_PATH", tmp_path / "render.json")


# ------------------------------------------------------------------ fakes
class FakeTool:
    def __init__(self, reg_id):
        self.reg_id = reg_id

    def GetAttrs(self):
        return {"TOOLS_RegID": self.reg_id}


class FakeComp:
    def __init__(self, *ids):
        self.tools = {i: FakeTool(r) for i, r in enumerate(ids, 1)}

    def GetToolList(self):
        return self.tools


class FakeGraph:
    def __init__(self, n):
        self.n = n

    def GetNumNodes(self):
        return self.n


class FakeItem:
    def __init__(self, name, start, end, ms_per_frame, comps=(), nodes=1):
        self.name, self.start, self.end = name, start, end
        self.ms_per_frame, self.comps, self.nodes = ms_per_frame, list(comps), nodes

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetDuration(self):
        return self.end - self.start

    def GetFusionCompCount(self):
        return len(self.comps)

    def GetFusionCompByIndex(self, i):
        return self.comps[i - 1]

    def GetNodeGraph(self):
        return FakeGraph(self.nodes)


class FakeTimeline:
    def __init__(self, tracks):
        self.tracks = tracks            # list of lists of FakeItem, index 0 = V1
        self.timecode = "01:00:17:08"

    def GetName(self):
        return "Timeline 1"

    def GetSetting(self, key):
        return {"timelineFrameRate": 60.0}.get(key)

    def GetTrackCount(self, kind):
        return len(self.tracks)

    def GetItemListInTrack(self, kind, index):
        return self.tracks[index - 1]

    def GetCurrentTimecode(self):
        return self.timecode

    def SetCurrentTimecode(self, tc):
        self.timecode = tc
        return True


class FakeProject:
    """Renders instantly; the cost per frame is looked up from the item covering MarkIn."""

    OVERHEAD_MS = 400            # the fixed per-job cost the live Resolve showed

    def __init__(self, timeline, codecs=None, fail_ranges=()):
        self.timeline = timeline
        self.codecs = codecs if codecs is not None else {"mov": {"DNxHR LB": "DNxHRLB", "H.264": "H264"},
                                                         "mp4": {"H.264": "H264"}}
        self.format = {"format": "mp4", "codec": "H264"}
        self.settings = {}
        self.jobs = {"user-job": {"JobStatus": "Ready"}}
        self.rendering = False
        self.deleted = []
        self.fail_ranges = set(fail_ranges)
        self.log = []

    def GetCurrentTimeline(self):
        return self.timeline

    def IsRenderingInProgress(self):
        return self.rendering

    def GetRenderCodecs(self, fmt):
        return self.codecs.get(fmt, {})

    def GetCurrentRenderFormatAndCodec(self):
        return dict(self.format)

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        self.format = {"format": fmt, "codec": codec}
        self.log.append(("format", fmt, codec))
        return True

    def SetRenderSettings(self, settings):
        self.settings.update(settings)
        return True

    def AddRenderJob(self):
        job = f"job{len(self.jobs)}"
        self.jobs[job] = {"JobStatus": "Ready", "mark_in": self.settings["MarkIn"],
                          "mark_out": self.settings["MarkOut"], "name": self.settings["CustomName"]}
        return job

    def StartRendering(self, jobs, interactive=False):
        for job in jobs:
            info = self.jobs[job]
            frames = info["mark_out"] - info["mark_in"] + 1
            item = self._item_at(info["mark_in"])
            if (info["mark_in"], info["mark_out"]) in self.fail_ranges:
                info["JobStatus"] = "Failed"
            else:
                info["JobStatus"] = "Complete"
                info["TimeTakenToRenderInMs"] = int(self.OVERHEAD_MS + frames * item.ms_per_frame)
            self.timeline.timecode = f"frame {info['mark_in']}"        # the real one moves the playhead
            self.page = "deliver"
            with open(os.path.join(self.settings["TargetDir"], info["name"] + ".mov"), "w") as fh:
                fh.write("x")
        return True

    def _item_at(self, frame):
        for track in reversed(self.timeline.tracks):
            for item in track:
                if item.start <= frame < item.end:
                    return item
        raise AssertionError(f"no item at {frame}")

    def GetRenderJobStatus(self, job):
        return dict(self.jobs[job])

    def DeleteRenderJob(self, job):
        self.deleted.append(job)
        del self.jobs[job]
        return True


class FakeResolve:
    def __init__(self, project):
        self.project = project
        self.page = "edit"
        project.page = "edit"

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project

    def GetCurrentPage(self):
        return self.project.page

    def OpenPage(self, page):
        self.project.page = page
        return True


def make(tracks, **kw):
    project = FakeProject(FakeTimeline(tracks), **kw)
    return FakeResolve(project), project


# ------------------------------------------------------------ intervals
def test_subtract_and_merge():
    assert _subtract((0, 100), []) == [(0, 100)]
    assert _subtract((0, 100), [(20, 40), (60, 70)]) == [(0, 20), (40, 60), (70, 100)]
    assert _subtract((10, 20), [(0, 100)]) == []
    assert _merge([(50, 60), (0, 10), (10, 20), (55, 70)]) == [(0, 20), (50, 70)]


def test_choose_format_prefers_dnxhr_lb_then_h264_then_current():
    _, project = make([[]])
    assert choose_format(project) == ("mov", "DNxHRLB")
    project.codecs = {"mp4": {"H.264": "H264"}}
    assert choose_format(project) == ("mp4", "H264")
    project.codecs = {}
    project.format = {"format": "mxf", "codec": "DNxHRSQ"}
    assert choose_format(project) == ("mxf", "DNxHRSQ")


# ---------------------------------------------------------------- queue
def test_render_queue_samples_and_restores_everything(tmp_path):
    resolve, project = make([[FakeItem("a", 216000, 217029, 32.0)]])
    project.format = {"format": "mov", "codec": "H264"}
    with RenderQueue(resolve, target_dir=str(tmp_path)) as queue:
        assert project.format == {"format": "mov", "codec": "DNxHRLB"}
        ms, status = queue.render_range(216500, 216523)
        assert (ms, status) == (400 + 24 * 32.0, "Complete")
        assert project.settings["MarkIn"] == 216500 and project.settings["MarkOut"] == 216523
        assert project.settings["SelectAllFrames"] is False
    # restored
    assert project.format == {"format": "mov", "codec": "H264"}
    assert project.settings["SelectAllFrames"] is True
    assert resolve.GetCurrentPage() == "edit"
    assert project.timeline.timecode == "01:00:17:08"
    assert list(project.jobs) == ["user-job"]                      # ours deleted, theirs kept
    assert project.deleted == ["job1"]
    assert list(tmp_path.iterdir()) == []                          # output removed


def test_render_queue_refuses_while_rendering(tmp_path):
    resolve, project = make([[FakeItem("a", 0, 100, 1.0)]])
    project.rendering = True
    with pytest.raises(RuntimeError, match="already rendering"):
        with RenderQueue(resolve, target_dir=str(tmp_path)):
            pass


def test_render_queue_cleans_own_temp_dir():
    resolve, _ = make([[FakeItem("a", 0, 100, 1.0)]])
    queue = RenderQueue(resolve)
    assert queue.target_dir is None                              # nothing on disk until entered
    with queue:
        assert os.path.isdir(queue.target_dir)
    assert not os.path.exists(queue.target_dir)


def test_render_cost_leaves_resolve_alone_when_every_clip_is_too_short(tmp_path):
    resolve, project = make([[FakeItem("blip", 0, 5, 2.0), FakeItem("blop", 5, 50, 2.0)]])
    project.format = {"format": "mov", "codec": "H264"}
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "q")))
    assert all(s.too_short for s in rc.samples)
    assert project.log == [] and project.settings == {} and project.deleted == []
    assert project.format == {"format": "mov", "codec": "H264"}
    assert not (tmp_path / "q").exists()


# ---------------------------------------------------------- render_cost
def test_sample_sizes():
    assert sample_sizes(6000, 60.0, 10.0, 2.0) == (600, 120)
    assert sample_sizes(6000, 24.0, 10.0, 2.0) == (240, 48)
    assert sample_sizes(6000, 24.0, 1.0, 2.0) == (120, 30)        # never below MIN_LONG_FRAMES
    assert sample_sizes(200, 60.0, 10.0, 2.0) == (200, 50)         # clip shorter than 10 s
    assert sample_sizes(10, 60.0, 10.0, 2.0) == (10, 0)            # too short for two samples
    assert sample_sizes(6000, 60.0, 10.0, 0.0) == (600, 0)         # short sample disabled


def test_render_cost_samples_middle_of_each_clip_on_every_track(tmp_path):
    v1 = [FakeItem("plain", 0, 6000, 10.0), FakeItem("graded", 6000, 12000, 40.0, nodes=6)]
    v2 = [FakeItem("title", 3000, 4200, 25.0, comps=[FakeComp("MediaIn", "Text+", "MediaOut")])]
    resolve, project = make([v1, v2])
    log = []
    rc = render_cost(resolve, progress=log.append,
                     queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    assert [s.item for s in rc.samples] == ["plain", "graded", "title"]
    assert [s.sample_start for s in rc.samples] == [2700, 8700, 3300]  # (length-600)//2 in
    assert all(s.frames == 600 and s.short_frames == 120 and s.ok for s in rc.samples)
    # the two-point fit cancels the 400 ms per-job overhead exactly
    assert [round(s.ms_per_frame) for s in rc.samples] == [10, 40, 25]
    assert all(round(s.overhead_ms) == 400 for s in rc.samples)
    assert "overhead ~400 ms removed" in rc.text()
    assert [l for l in log if "short" in l] and [l for l in log if "long" in l]
    assert rc.samples[2].fusion_tools == ["Text+"] and rc.samples[1].color_nodes == 6
    assert rc.fps == 60.0 and rc.codec == "DNxHRLB"


def test_long_sample_is_cut_back_for_a_heavy_clip(tmp_path):
    # 2 s per frame: a 600-frame sample would take 20 minutes. Budget 60 s -> ~30 frames,
    # but never below 4x the short sample.
    resolve, project = make([[FakeItem("fusion", 0, 6000, 2000.0)]])
    rc = render_cost(resolve, budget_s=60.0, queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    s = rc.samples[0]
    assert s.short_frames == 120 and s.frames == 480               # 4 * short wins over budget
    assert round(s.ms_per_frame) == 2000
    resolve, project = make([[FakeItem("fusion", 0, 6000, 2000.0)]])
    rc = render_cost(resolve, budget_s=600.0, short_seconds=0.5,     # ~300 frames affordable
                     queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    s = rc.samples[0]
    assert s.short_frames == 30 and 120 <= s.frames < 600            # budget-limited


def test_render_cost_does_not_measure_clips_under_the_slope_floor(tmp_path):
    # A 5-frame job is almost all set-up time: (400 + 5 * 2) / 5 = 82 ms/frame for a 2 ms
    # clip. Rather than report that as heavy, the clip is skipped and says so.
    resolve, project = make([[FakeItem("blip", 0, 5, 2.0), FakeItem("long", 5, 6005, 2.0)]])
    log = []
    rc = render_cost(resolve, progress=log.append, queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    blip, long = rc.samples
    assert blip.too_short and not blip.ok and blip.frames == 0
    assert long.ok and round(long.ms_per_frame) == 2
    assert len(project.deleted) == 2                             # only the long clip was rendered
    assert log[0] == "1 clip(s) under 120 frames - too short to measure alone"
    assert rc.total_frames == 6000                               # not in the export estimate
    assert "too short to measure" in rc.text() and "1 clip(s) under 120 frames not measured" in rc.text()
    assert [f.subject for f in render_findings(rc)] == []        # no finding, not a failure either


def cuts(name, start, count, length, ms, **kw):
    """``count`` back-to-back clips of ``length`` frames from ``start``."""
    return [FakeItem(f"{name}{k}", start + k * length, start + (k + 1) * length, ms, **kw)
            for k in range(count)]


def test_plan_stretches_covers_runs_of_short_clips_and_nothing_else():
    items = [{"name": f"c{k}", "track": 1, "start": k * 50, "end": (k + 1) * 50, "path": f"/{k}",
              "fusion_tools": ["Blur"] if k == 3 else [], "color_nodes": 1 + k % 3} for k in range(30)]
    items.append({"name": "long", "track": 1, "start": 1500, "end": 3000, "path": "/L",
                  "fusion_tools": [], "color_nodes": 1})
    items.append({"name": "lone", "track": 1, "start": 3000, "end": 3050, "path": "/x",
                  "fusion_tools": [], "color_nodes": 1})
    out = plan_stretches(items, 60.0, seconds=10.0)               # target 600 frames
    assert [(s["start"], s["end"]) for s in out] == [(0, 750), (750, 1500)]   # 1500 // 600 = 2 pieces
    first = out[0]
    assert first["label"] == "stretch @ 00:00:00:00 (15 clips)" and first["track"] == 0
    assert first["fusion_tools"] == ["Blur"] and first["color_nodes"] == 3
    assert first["clips"][:2] == ["c0 @V1 00:00:00:00", "c1 @V1 00:00:00:50"]
    assert "/3@150-200" in first["path"]
    # a lone short clip with no short neighbours: nothing to stretch
    assert plan_stretches(items[-2:], 60.0) == []
    # a short clip under a long one on another track is already covered
    covered = [{"name": "ov", "track": 2, "start": 100, "end": 150, "path": "/o",
                "fusion_tools": [], "color_nodes": 1}, dict(items[-2], start=0, end=3000)]
    assert plan_stretches(covered, 60.0) == []


def test_render_cost_measures_fast_cut_sections_as_stretches(tmp_path):
    # 12 short clips (600 frames), a long one, then 4 more short ones (200 frames);
    # the first run is heavy throughout and one cut in it carries a Fusion tool.
    v1 = (cuts("a", 0, 12, 50, 90.0) + [FakeItem("long", 600, 1200, 4.0)] + cuts("b", 1200, 4, 50, 4.0))
    v1[6] = FakeItem("a6", 300, 350, 90.0, comps=[FakeComp("Blur")])
    resolve, project = make([v1])
    log = []
    rc = render_cost(resolve, progress=log.append, queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    assert log[0] == ("16 clip(s) under 120 frames - too short to measure alone; "
                      "measuring 2 stretch(es) of consecutive clips instead")
    stretches = [s for s in rc.samples if s.stretch]
    assert [s.label for s in stretches] == ["stretch @ 00:00:00:00 (12 clips)",
                                            "stretch @ 00:00:20:00 (4 clips)"]
    s1, s2 = stretches
    assert s1.ok and s1.frames == 600 and s1.sample_start == 0 and s1.fusion_tools == ["Blur"]
    assert s2.ok and s2.frames == 200 and s2.short_frames == 50
    assert round(s1.ms_per_frame) == 90
    assert round(s2.ms_per_frame) == 4
    assert all(s.in_stretch for s in rc.samples if s.too_short)
    assert rc.samples[0].in_stretch == s1.label and rc.samples[16].in_stretch == s2.label
    assert len(project.deleted) == 6                             # long clip + 2 stretches, two jobs each
    # export: 600 (stretch 1) + 600 (long) + 200 (stretch 2) frames, nothing double counted
    assert rc.total_frames == 1400
    assert round(rc.estimated_export_s, 1) == round((600 * 90 + 600 * 4 + 200 * 4) / 1000, 1)
    assert rc.shares[s1.label] > 0.9
    text = rc.text()
    assert "measured in a stretch (below)" in text and "16 clip(s) under 120 frames measured as 2 stretch(es)" in text
    assert "stretch @ 00:00:00:00 (12 clips)     -     600      90.0" in text
    findings = render_findings(rc)
    heavy = [f for f in findings if f.code == "render-heavy"]
    assert [f.subject for f in heavy] == [s1.label]
    assert heavy[0].message == "renders at 0.19x real time (90 ms/frame) across 12 short clips"
    assert "average over the clips it spans" in heavy[0].why and "Blur" in heavy[0].why
    data = rc.to_dict()
    assert data["samples"][-2]["stretch"] is True and data["samples"][-2]["clips"][0] == "a0 @V1 00:00:00:00"
    assert data["samples"][0]["stretch"] is False

    # a re-cut inside the run changes the stretch's cache key
    v1[2] = FakeItem("a2", 100, 130, 4.0)
    v1.insert(3, FakeItem("a2b", 130, 150, 4.0))
    resolve, project = make([v1])
    rc2 = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "b")))
    again = [s for s in rc2.samples if s.stretch]
    assert not again[0].from_cache and again[1].from_cache


def test_stretch_with_a_finding_gets_one_marker_spanning_it(tmp_path):
    from renderflow.fix import plan
    from renderflow.scan import ProjectSettings, ScanReport, TimelineInfo
    items = [{"name": f"c{k}", "track": 1, "start": 216000 + k * 50, "end": 216050 + k * 50,
              "clip": "c.mp4", "label": f"c{k} @V1 x", "fusion_tools": [], "color_nodes": 1}
             for k in range(4)]
    tl = TimelineInfo("Timeline 1", 60.0, 1920, 1080, 1, 4, 216000, items)
    stretch = RenderSample("stretch of 4 clips", 0, 216000, 216200, 216000, 200, 90.0 * 200, "Complete",
                           label="stretch @ 01:00:00:00 (4 clips)", clips=[i["label"] for i in items],
                           fusion_tools=["Blur"])
    rp = RenderProfile("Timeline 1", 60.0, "mov", "DNxHRLB", [stretch])
    estimate_export(rp)
    settings = ProjectSettings("1", "", "none", "", False, "", 1)
    rep = ScanReport("p", "win32", tl, settings, [])
    actions = plan(rep, rp, render_findings(rp))
    (cache, marker) = actions
    assert cache.params["key"] == "perfRenderCacheMode" and cache.params["value"] == "smart"
    assert marker.kind == "marker" and marker.subject == stretch.label
    assert marker.params["frame"] == 0 and marker.params["duration"] == 200
    assert marker.params["note"].startswith("render-heavy: renders at 0.19x real time")


def test_render_cost_can_skip_the_short_sample(tmp_path):
    resolve, project = make([[FakeItem("a", 0, 6000, 10.0)]])
    rc = render_cost(resolve, short_seconds=0,
                     queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    assert rc.samples[0].short_frames == 0 and len(project.deleted) == 1


def test_export_estimate_charges_each_frame_to_the_top_track_once(tmp_path):
    v1 = [FakeItem("plain", 0, 6000, 10.0), FakeItem("graded", 6000, 12000, 40.0)]
    v2 = [FakeItem("title", 3000, 4200, 25.0)]
    resolve, _ = make([v1, v2])
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    # plain: 6000 frames minus the 1200 under the title = 4800 * 10; title 1200 * 25; graded 6000 * 40
    expected_ms = 4800 * 10 + 1200 * 25 + 6000 * 40
    assert rc.total_frames == 12000
    assert rc.estimated_export_s == round(expected_ms / 1000, 1)
    shares = {s.item: rc.shares[s.label] for s in rc.samples}
    assert round(shares["graded"], 3) == round(240000 / expected_ms, 3)
    assert rc.samples[0].label == "plain @V1 00:00:00:00"
    assert abs(sum(rc.shares.values()) - 1.0) < 1e-9


def test_failed_sample_is_recorded_not_raised(tmp_path):
    resolve, _ = make([[FakeItem("bad", 0, 1000, 1.0), FakeItem("good", 1000, 2000, 1.0)]],
                      fail_ranges={(200, 799)})          # the 600-frame long sample of "bad"
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path)))
    assert rc.samples[0].status == "Failed" and not rc.samples[0].ok
    assert rc.samples[1].ok
    assert rc.total_frames == 1000                                 # failed clip not estimated
    codes = [f.code for f in render_findings(rc)]
    assert "render-sample-failed" in codes


# ----------------------------------------------------------------- cache
def test_render_cost_reuses_cached_samples_and_keys_on_what_matters(tmp_path):
    v1 = [FakeItem("plain", 0, 6000, 10.0), FakeItem("blip", 6000, 6010, 1.0)]
    resolve, project = make([v1])
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "a")))
    assert not rc.samples[0].from_cache and rc.samples[0].measured_at > 0
    assert (tmp_path / "render.json").exists()
    rendered = len(project.deleted)

    # same timeline again: nothing rendered, the number is the same, and the text says so
    resolve, project = make([v1])
    log = []
    rc2 = render_cost(resolve, progress=log.append, queue=RenderQueue(resolve, target_dir=str(tmp_path / "b")))
    assert project.deleted == [] and rc2.samples[0].from_cache
    assert project.log == [] and project.settings == {}          # queue never entered: nothing touched
    assert not (tmp_path / "b").exists()
    assert round(rc2.samples[0].ms_per_frame) == 10 and rc2.samples[1].too_short
    assert "1 sample(s) reused from an earlier run" in rc2.text()
    assert any(l.endswith("plain - cached") for l in log)
    assert rc2.to_dict()["samples"][0]["from_cache"] is True

    # a Fusion tool added to the clip changes the key -> rendered again
    resolve, project = make([[FakeItem("plain", 0, 6000, 10.0, comps=[FakeComp("Blur")])]])
    rc3 = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "c")))
    assert not rc3.samples[0].from_cache and len(project.deleted) == rendered

    # --remeasure: an in-memory cache renders again and writes nothing
    resolve, project = make([v1])
    rc4 = render_cost(resolve, cache=RenderCache(None), queue=RenderQueue(resolve, target_dir=str(tmp_path / "d")))
    assert not rc4.samples[0].from_cache and len(project.deleted) == rendered


def test_render_cache_ignores_failed_samples_and_bad_files(tmp_path):
    cache = RenderCache(tmp_path / "r.json")
    cache.put("k", RenderSample("x", 1, 0, 600, 0, 600, 0.0, "Failed"))
    assert cache.get("k") is None and not (tmp_path / "r.json").exists()
    (tmp_path / "r.json").write_text("{not json")
    assert RenderCache(tmp_path / "r.json").data == {}


# -------------------------------------------------------------- findings
def sample(name, ms, start=0, end=600, track=1, **kw):
    return RenderSample(name, track, start, end, start, 24, ms * 24, "Complete", label=name, **kw)


def test_findings_thresholds_and_relative_cost():
    rc = RenderProfile("t", 60.0, "mov", "DNxHRLB",
                       [sample("fast", 5.0), sample("slow", 30.0, 600, 1200), sample("heavy", 100.0, 1200, 1800)])
    estimate_export(rc)
    found = {(f.subject, f.code): f for f in render_findings(rc)}
    assert not any(subject == "fast" for subject, _ in found)     # 200 fps on 60 fps timeline
    assert found[("slow", "render-slow")].severity == "medium"
    heavy = found[("heavy", "render-heavy")]
    assert heavy.severity == "high" and heavy.message.endswith("0.17x real time (100 ms/frame)")
    dominant = [f for f in render_findings(rc) if f.code == "export-dominant"]
    assert [f.subject for f in dominant] == ["heavy"]              # 100/(5+30+100) = 74%


def test_findings_mention_what_the_clip_carries():
    rc = RenderProfile("t", 60.0, "mov", "DNxHRLB",
                       [sample("fx", 80.0, fusion_tools=["Blur", "Glow"], color_nodes=5)])
    estimate_export(rc)
    (finding,) = [f for f in render_findings(rc) if f.code == "render-heavy"]
    assert "Blur, Glow" in finding.why and "5-node grade" in finding.why


def test_text_and_dict_output():
    rc = RenderProfile("Timeline 1", 60.0, "mov", "DNxHRLB",
                       [sample("a", 32.1, 216000, 217029), RenderSample("b", 1, 0, 10, 0, 10, 0, "Failed")])
    estimate_export(rc)
    text = rc.text()
    assert "Timeline 1 @ 60 fps" in text and "0.52x" in text and "Failed" in text
    assert "estimated export" in text and "1029 frames" in text
    data = rc.to_dict()
    assert data["samples"][0]["realtime_ratio"] == 0.519 and data["samples"][0]["export_share"] == 1.0
    assert data["samples"][1]["ms_per_frame"] == 0
