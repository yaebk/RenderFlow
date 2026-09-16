"""Fixer tests: planning policy, journaled apply, and undo - against a fake
Resolve that records every call."""

import json
import os
import subprocess

import pytest

from renderflow.fix import Action, Journal, apply, plan, plan_text, undo
from renderflow.rendercost import RenderProfile, RenderSample
from renderflow.report import full_report
from renderflow.scan import (
    Finding,
    ProjectSettings,
    ScanReport,
    TimelineInfo,
    clip_from_properties,
    find_issues,
    item_label,
)


# ------------------------------------------------------------------ data
def clip(name="cam.mp4", path=r"D:\f\cam.mp4", **over):
    base = {"Clip Name": name, "File Path": path, "Type": "Video + Audio",
            "Video Codec": "H.264 High", "Resolution": "3840x2160", "FPS": 60.0,
            "Bit Depth": "8", "Frames": "3600", "Online Status": "Online", "Proxy": "None",
            "Usage": "1", "Super Scale": 1, "Start TC": "01:00:00:00"}
    base.update(over)
    return clip_from_properties(base, exists=lambda p: True, drive_type=lambda p: 3, size_of=lambda p: 1)


def settings(**over):
    base = dict(proxy_mode="2", proxy_resolution="original", render_cache_mode="none",
                render_cache_codec="dnx", optimized_media_on=True, optimized_codec="dnx", super_scale=1)
    base.update(over)
    return ProjectSettings(**base)


def report(*clips, items=None, **setting_over):
    items = items if items is not None else [
        {"name": c.name, "track": 1, "start": 216000 + i * 600, "end": 216600 + i * 600,
         "clip": c.name, "label": item_label(c.name, 1, 216000 + i * 600, 60.0),
         "fusion_tools": [], "color_nodes": 1}
        for i, c in enumerate(clips)]
    for c in clips:
        c.on_timeline = True
    tl = TimelineInfo("Timeline 1", 60.0, 1920, 1080, 1, len(items), 216000, items)
    rep = ScanReport("wowo", "win32", tl, settings(**setting_over), list(clips))
    rep.findings = find_issues(rep.clips, tl, rep.settings, "win32")
    return rep


def measured(c, ratio):
    c.measured = {"realtime_ratio": ratio, "decode_fps": ratio * 60, "seek_ms": 100.0}
    return c


def render_profile(*samples, fps=60.0):
    rp = RenderProfile("Timeline 1", fps, "mov", "DNxHRLB", list(samples))
    from renderflow.rendercost import estimate_export
    estimate_export(rp)
    return rp


def sample(name, ms, start=216000, end=216600, **kw):
    return RenderSample(name, 1, start, end, start, 600, ms * 600, "Complete",
                        label=item_label(name, 1, start, 60.0), **kw)


# ------------------------------------------------------------------ plan
def test_plan_nothing_when_measurements_are_fine():
    rep = report(measured(clip(), 6.0))
    assert plan(rep) == []
    assert "nothing to fix" in plan_text([])


def test_plan_proxy_for_measured_slow_clip_and_prefer_proxies_setting(tmp_path):
    rep = report(measured(clip(), 0.8), measured(clip("ok.mp4", r"D:\f\ok.mp4"), 4.0))
    actions = plan(rep, proxy_dir=tmp_path)
    kinds = [(a.kind, a.subject) for a in actions]
    assert ("proxy", "cam.mp4") in kinds and ("proxy", "ok.mp4") not in kinds
    assert ("setting", "project") in kinds
    proxy = next(a for a in actions if a.kind == "proxy")
    assert (proxy.params["width"], proxy.params["height"]) == (1920, 1080)      # 4K -> HD
    assert proxy.params["target"].startswith(str(tmp_path)) and proxy.params["timecode"] == "01:00:00:00"
    assert proxy.estimate_s == pytest.approx(60 / 4.0)
    mode = next(a for a in actions if a.kind == "setting")
    assert mode.params == {"key": "perfProxyMediaMode", "value": "1", "old": "2"}


def test_plan_proxies_all_takes_unmeasured_long_gop_but_not_on_macos_or_intra():
    rep = report(clip(), clip("pr.mov", r"D:\f\pr.mov", **{"Video Codec": "Apple ProRes 422"}))
    assert [a.subject for a in plan(rep, proxies="all") if a.kind == "proxy"] == ["cam.mp4"]
    assert [a for a in plan(rep, proxies="all", platform="darwin") if a.kind == "proxy"] == []
    assert [a for a in plan(rep, proxies="none") if a.kind == "proxy"] == []
    assert [a for a in plan(rep) if a.kind == "proxy"] == []           # auto needs a measurement


def test_plan_skips_clips_that_already_have_proxies_but_still_fixes_the_mode():
    rep = report(measured(clip(**{"Proxy": "Half", "Proxy Media Path": r"D:\p\x.mov"}), 0.5))
    actions = plan(rep)
    assert [a.kind for a in actions] == ["setting"]
    assert actions[0].params["key"] == "perfProxyMediaMode"
    rep = report(measured(clip(**{"Proxy": "Half"}), 0.5), proxy_mode="1")
    assert plan(rep) == []


def test_plan_super_scale_project_and_clip():
    rep = report(clip(**{"Super Scale": 2}), super_scale=3)
    actions = plan(rep, markers=False)
    assert [(a.kind, a.params["key"], a.params["value"]) for a in actions] == [
        ("setting", "superScale", "1"), ("clip-setting", "Super Scale", 1)]


def test_plan_smart_cache_only_for_heavy_effect_clips():
    rep = report(clip("fx.mp4"), clip("plain.mp4", r"D:\f\plain.mp4"))
    rp = render_profile(sample("fx.mp4", 40.0, fusion_tools=["Blur"]),
                        sample("plain.mp4", 40.0, 216600, 217200))          # slow but carries nothing
    actions = [a for a in plan(rep, rp, markers=False) if a.kind == "setting"]
    assert len(actions) == 1 and actions[0].params["key"] == "perfRenderCacheMode"
    assert "fx.mp4" in actions[0].why and "plain" not in actions[0].why
    assert plan(report(clip("fx.mp4"), render_cache_mode="smart"), rp, markers=False) == []


def test_plan_markers_from_high_and_medium_findings_only():
    rep = report(clip("a.mp4"), clip("b.mp4", r"D:\f\b.mp4"), clip("c.mp4", r"D:\f\c.mp4"))
    findings = [Finding("high", "render-heavy", "a.mp4", "slow", "why"),
                Finding("medium", "fusion-comp", "a.mp4", "fusion", "why"),
                Finding("info", "decode-ok", "b.mp4", "fine", "why"),
                Finding("medium", "seek-slow", "c.mp4", "sticky", "why")]
    actions = plan(rep, findings=findings, settings=False)
    assert [(a.kind, a.subject, a.params["color"]) for a in actions] == [
        ("marker", "a.mp4 @V1 01:00:00:00", "Red"), ("marker", "c.mp4 @V1 01:00:20:00", "Yellow")]
    a = actions[0].params
    assert a["frame"] == 0 and a["duration"] == 600 and a["custom"] == "renderflow:216000"
    assert "render-heavy" in a["note"] and "fusion-comp" in a["note"]
    assert actions[1].params["frame"] == 1200


def test_plan_one_marker_per_frame_and_skips_occupied_frames():
    # Resolve allows one marker per frame: items on other tracks that start together
    # share a marker; frames that already carry one (ours or the editor's) are skipped.
    items = [
        {"name": "cam.mp4", "track": 1, "start": 216000, "end": 216600, "clip": "cam.mp4",
         "label": item_label("cam.mp4", 1, 216000, 60.0), "fusion_tools": [], "color_nodes": 1},
        {"name": "Adjustment Clip", "track": 3, "start": 216000, "end": 216900, "clip": "",
         "label": item_label("Adjustment Clip", 3, 216000, 60.0), "fusion_tools": [], "color_nodes": 1},
        {"name": "old.mp4", "track": 1, "start": 216600, "end": 217200, "clip": "old.mp4",
         "label": item_label("old.mp4", 1, 216600, 60.0), "fusion_tools": [], "color_nodes": 1},
        {"name": "blue.mp4", "track": 1, "start": 217200, "end": 217800, "clip": "blue.mp4",
         "label": item_label("blue.mp4", 1, 217200, 60.0), "fusion_tools": [], "color_nodes": 1},
    ]
    rep = report(clip("cam.mp4"), items=items)
    rep.timeline.markers = {600: "renderflow:216600", 1200: ""}
    findings = [Finding("medium", "decode-marginal", "cam.mp4", "slowish", "why"),
                Finding("high", "render-heavy", items[1]["label"], "heavy", "why"),
                Finding("high", "render-heavy", "old.mp4", "heavy", "why"),
                Finding("high", "render-heavy", "blue.mp4", "heavy", "why")]
    notes = []
    actions = plan(rep, findings=findings, settings=False, notes=notes)
    assert len(actions) == 1
    assert notes == ["1 finding(s) already marked by an earlier run.",
                     "1 finding(s) not marked - the frame already has a marker of your own: "
                     "blue.mp4 @V1 01:00:20:00."]
    text = plan_text(actions, notes)
    assert text.endswith("         " + notes[1]) and "1 change(s) planned" in text
    assert plan_text([], notes).startswith("nothing to fix") and notes[0] in plan_text([], notes)
    a = actions[0]
    assert a.subject == "cam.mp4 @V1 01:00:00:00 (+1 more)"
    assert a.params["color"] == "Red" and a.params["frame"] == 0 and a.params["duration"] == 900
    assert "V1 cam.mp4: decode-marginal" in a.params["note"]
    assert "V3 Adjustment Clip: render-heavy" in a.params["note"]
    assert a.params["custom"] == "renderflow:216000"


# ----------------------------------------------------------------- fakes
class FakeItem:
    def __init__(self, path):
        self.path = path
        self.props = {"File Path": path, "Super Scale": 2}
        self.proxy = None

    def GetClipProperty(self, key=None):
        return self.props if key is None else self.props.get(key)

    def SetClipProperty(self, key, value):
        self.props[key] = value
        return True

    def LinkProxyMedia(self, path):
        self.proxy = path
        return os.path.exists(path)

    def UnlinkProxyMedia(self):
        self.proxy = None
        return True


class FakeFolder:
    def __init__(self, items):
        self.items = items

    def GetClipList(self):
        return self.items

    def GetSubFolderList(self):
        return []


class FakeTimeline:
    def __init__(self):
        self.markers = {}

    def GetName(self):
        return "Timeline 1"

    def GetMarkers(self):
        return dict(self.markers)

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        if frame in self.markers:                                   # one marker per frame, like Resolve
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration,
                               "customData": custom}
        return True

    def DeleteMarkerByCustomData(self, custom):
        for frame, m in list(self.markers.items()):
            if m["customData"] == custom:
                del self.markers[frame]
                return True
        return False


class FakeProject:
    def __init__(self, items, name="wowo"):
        self.name = name
        self.folder = FakeFolder(items)
        self.settings = {"perfProxyMediaMode": "2", "superScale": 3, "perfRenderCacheMode": "none"}
        self.timeline = FakeTimeline()

    def GetName(self):
        return self.name

    def GetMediaPool(self):
        return self

    def GetRootFolder(self):
        return self.folder

    def GetCurrentTimeline(self):
        return self.timeline

    def GetSetting(self, key):
        return self.settings.get(key)

    def SetSetting(self, key, value):
        self.settings[key] = value
        return True


class FakeResolve:
    def __init__(self, project):
        self.project = project

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project


def fake_ffmpeg(cmd):
    """Pretend to encode: create the target file."""
    with open(cmd[-1], "w") as fh:
        fh.write("proxy")
    return subprocess.CompletedProcess(cmd, 0, "", "")


# ----------------------------------------------------------- apply/undo
def test_apply_then_undo_round_trip(tmp_path):
    item = FakeItem(r"D:\f\cam.mp4")
    project = FakeProject([item])
    resolve = FakeResolve(project)
    rep = report(measured(clip(**{"Super Scale": 2}), 0.5), super_scale=3)
    findings = [Finding("high", "decode-below-realtime", "cam.mp4", "slow", "why")]
    actions = plan(rep, findings=findings, proxy_dir=tmp_path / "proxies")
    assert [a.kind for a in actions] == ["proxy", "setting", "setting", "clip-setting", "marker"]

    journal = Journal(tmp_path / "journal.json")
    log = []
    problems = apply(resolve, actions, journal, progress=log.append, ffmpeg="ffmpeg",
                     proxy_runner=fake_ffmpeg)
    assert problems == []
    assert item.proxy and os.path.exists(item.proxy)
    assert project.settings == {"perfProxyMediaMode": "1", "superScale": "1", "perfRenderCacheMode": "none"}
    assert item.props["Super Scale"] == 1
    assert list(project.timeline.markers) == [0]
    assert len(journal) == 5
    saved = json.loads((tmp_path / "journal.json").read_text())
    assert [e["kind"] for e in saved["entries"]] == ["proxy", "setting", "setting", "clip-setting", "marker"]
    assert saved["entries"][1] == {**saved["entries"][1], "key": "perfProxyMediaMode", "old": "2"}
    assert all(e["project"] == "wowo" for e in saved["entries"])

    problems = undo(resolve, Journal(tmp_path / "journal.json"), progress=log.append)
    assert problems == []
    assert item.proxy is None and not os.path.exists(actions[0].params["target"])
    assert project.settings == {"perfProxyMediaMode": "2", "superScale": 3, "perfRenderCacheMode": "none"}
    assert item.props["Super Scale"] == 2
    assert project.timeline.markers == {}
    assert len(Journal(tmp_path / "journal.json")) == 0


def test_apply_records_problems_and_keeps_going(tmp_path):
    project = FakeProject([])                                   # clip missing from the pool
    resolve = FakeResolve(project)
    actions = [Action("clip-setting", "gone.mp4", "x", "y", {"path": r"D:\gone.mp4", "key": "Super Scale", "value": 1}),
               Action("setting", "project", "x", "y", {"key": "superScale", "value": "1"})]
    journal = Journal(tmp_path / "j.json")
    problems = apply(resolve, actions, journal)
    assert len(problems) == 1 and "gone.mp4" in problems[0]
    assert project.settings["superScale"] == "1" and len(journal) == 1


def test_apply_names_the_marker_in_the_way(tmp_path):
    project = FakeProject([])
    project.timeline.AddMarker(0, "Blue", "Marker 1", "", 1)
    action = Action("marker", "cam.mp4", "x", "y", {"frame": 0, "duration": 5, "color": "Red",
                    "name": "RenderFlow: render-heavy", "note": "n", "custom": "renderflow:216000",
                    "timeline": "Timeline 1"})
    problems = apply(FakeResolve(project), [action], Journal(tmp_path / "j.json"))
    assert problems == ["marker cam.mp4: AddMarker refused: frame 0 already has marker 'Marker 1'"]


def test_undo_tolerates_a_marker_the_editor_already_deleted(tmp_path):
    project = FakeProject([])
    journal = Journal(tmp_path / "j.json")
    journal.add({"kind": "marker", "subject": "cam.mp4", "custom": "renderflow:216000",
                 "timeline": "Timeline 1"})
    log = []
    assert undo(FakeResolve(project), journal, progress=log.append) == []
    assert log == ["  removed 0 marker(s) (1 already deleted by hand)"]
    assert Journal(tmp_path / "j.json").entries == []


def test_undo_sweeps_tagged_markers_the_journal_lost(tmp_path):
    project = FakeProject([])
    tl = project.timeline
    tl.AddMarker(0, "Red", "RenderFlow: render-heavy", "n", 5, "renderflow:216000")
    tl.AddMarker(9, "Red", "RenderFlow: fusion-comp", "n", 5, "renderflow:216009")
    tl.AddMarker(20, "Blue", "Marker 1", "", 1)                    # the editor's own: kept
    log = []
    assert undo(FakeResolve(project), Journal(tmp_path / "j.json"), progress=log.append) == []
    assert list(tl.markers) == [20]
    assert log == ["  removed 2 leftover RenderFlow marker(s) the journal did not know about"]


def test_undo_keeps_entries_it_could_not_reverse(tmp_path):
    project = FakeProject([])
    journal = Journal(tmp_path / "j.json")
    journal.add({"kind": "clip-setting", "subject": "gone.mp4", "path": r"D:\gone.mp4",
                 "key": "Super Scale", "old": 2})
    journal.add({"kind": "setting", "subject": "project", "key": "superScale", "old": 3})
    project.settings["superScale"] = "1"
    problems = undo(FakeResolve(project), journal)
    assert len(problems) == 1 and project.settings["superScale"] == 3
    assert [e["kind"] for e in Journal(tmp_path / "j.json").entries] == ["clip-setting"]


def test_undo_leaves_another_projects_changes_for_that_project(tmp_path):
    journal = Journal(tmp_path / "j.json")
    journal.add({"kind": "setting", "subject": "project", "key": "superScale", "old": 3,
                 "project": "wowo"})
    journal.add({"kind": "setting", "subject": "project", "key": "perfRenderCacheMode", "old": "none"})
    other = FakeProject([], name="other")
    other.settings.update({"superScale": "1", "perfRenderCacheMode": "smart"})
    problems = undo(FakeResolve(other), journal)
    assert problems == ["1 change(s) were made to project 'wowo', not 'other' - "
                        "open that project and run --undo again"]
    assert other.settings["superScale"] == "1"                  # wowo's change: untouched
    assert other.settings["perfRenderCacheMode"] == "none"      # unowned (old journal): undone
    assert [e["key"] for e in Journal(tmp_path / "j.json").entries] == ["superScale"]
    assert undo(FakeResolve(FakeProject([])), Journal(tmp_path / "j.json")) == []
    assert Journal(tmp_path / "j.json").entries == []


def test_journal_survives_corrupt_file(tmp_path):
    (tmp_path / "j.json").write_text("nope")
    assert Journal(tmp_path / "j.json").entries == []


# ---------------------------------------------------------------- report
def test_full_report_composes_stages_and_reports_skips(monkeypatch):
    rep = report(clip())
    monkeypatch.setattr("renderflow.report.scan", lambda resolve: rep)
    monkeypatch.setattr("renderflow.report.profile", lambda r, progress=None: {})
    rp = render_profile(sample("cam.mp4", 40.0, fusion_tools=["Blur"]))
    monkeypatch.setattr("renderflow.report.render_cost", lambda resolve, progress=None, **kw: rp)
    full = full_report(object())
    codes = [f.code for f in full.findings]
    assert "render-heavy" in codes and "codec-long-gop" in codes
    assert full.findings[0].severity == "high"
    kinds = {a.kind for a in full.actions}
    assert kinds == {"setting", "marker"}                       # smart cache + marker
    text = full.text()
    assert "RENDER (Resolve's queue)" in text and "PLAN" in text and "fix --apply" in text
    data = full.to_dict()
    assert data["render"]["samples"][0]["item"] == "cam.mp4" and len(data["actions"]) == 2

    monkeypatch.setattr("renderflow.report.render_cost",
                        lambda resolve, progress=None, **kw: (_ for _ in ()).throw(RuntimeError("busy")))
    full = full_report(object(), decode=False)
    assert full.render is None and any("busy" in s for s in full.skipped)
    assert "skipped:" in full.text()
