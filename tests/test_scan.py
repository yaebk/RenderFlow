"""Scan tests against a fake media pool. Property keys and values mirror what a
real Resolve 19 returned over the bridge, so the fakes are not invented."""

import json

from renderflow.scan import (
    ProjectSettings,
    TimelineInfo,
    classify_location,
    clip_from_properties,
    codec_family,
    find_issues,
    scan,
)


# ------------------------------------------------------------------ fakes
def props(**over):
    base = {
        "Clip Name": "cam.mp4", "File Name": "cam.mp4",
        "File Path": r"D:\footage\cam.mp4", "Type": "Video + Audio",
        "Video Codec": "H.264 High L4.2", "Format": "QuickTime",
        "Resolution": "1920x1080", "FPS": 60.0, "Bit Depth": "8", "Frames": "3605",
        "Online Status": "Online", "Proxy": "None", "Proxy Media Path": "",
        "Usage": "2", "Super Scale": 1,
    }
    base.update(over)
    return base


class FakeTool:
    def __init__(self, reg_id):
        self.reg_id = reg_id

    def GetAttrs(self):
        return {"TOOLS_RegID": self.reg_id}


class FakeComp:
    def __init__(self, *reg_ids):
        self.tools = {i + 1: FakeTool(r) for i, r in enumerate(reg_ids)}

    def GetToolList(self):
        return self.tools


class FakeGraph:
    def __init__(self, n):
        self.n = n

    def GetNumNodes(self):
        return self.n


class FakeMediaItem:
    def __init__(self, properties, uid):
        self.properties, self.uid = properties, uid

    def GetClipProperty(self, key=None):
        return self.properties if key is None else self.properties.get(key)

    def GetUniqueId(self):
        return self.uid


class FakeTimelineItem:
    def __init__(self, media, comps=(), nodes=1, start=216000, end=217029):
        self.media, self.comps, self.nodes = media, list(comps), nodes
        self.start, self.end = start, end

    def GetName(self):
        return self.media.properties.get("Clip Name", "")

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetMediaPoolItem(self):
        return self.media

    def GetFusionCompCount(self):
        return len(self.comps)

    def GetFusionCompByIndex(self, index):
        return self.comps[index - 1]

    def GetNodeGraph(self):
        return FakeGraph(self.nodes)


class FakeFolder:
    def __init__(self, name, clips=(), subs=()):
        self.name, self.clips, self.subs = name, list(clips), list(subs)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs


class FakeTimeline:
    def __init__(self, items, settings=None):
        self.items = items
        self.settings = {"timelineFrameRate": 60.0, "timelineResolutionWidth": "1920",
                         "timelineResolutionHeight": "1080", **(settings or {})}

    def GetName(self):
        return "Timeline 1"

    def GetStartFrame(self):
        return 216000

    def GetSetting(self, key):
        return self.settings.get(key)

    def GetTrackCount(self, kind):
        return 1

    def GetItemListInTrack(self, kind, index):
        return self.items if (kind, index) == ("video", 1) else []


class FakeProject:
    def __init__(self, root, timeline=None, settings=None):
        self.root, self.timeline = root, timeline
        self.settings = {"perfProxyMediaMode": "2", "perfProxyResolutionRatio": "original",
                         "perfRenderCacheMode": "none", "perfRenderCacheCodec": "dnxhd_hqx_12b",
                         "perfOptimisedMediaOn": "1", "perfOptimisedCodec": "dnxhd_hqx_12b",
                         "superScale": 1, **(settings or {})}

    def GetName(self):
        return "wowo"

    def GetMediaPool(self):
        return self

    def GetRootFolder(self):
        return self.root

    def GetCurrentTimeline(self):
        return self.timeline

    def GetSetting(self, key=None):
        return self.settings if key is None else self.settings.get(key)


class FakeResolve:
    def __init__(self, project, product="DaVinci Resolve"):
        self.project = project
        self.product = product

    def GetProductName(self):
        return self.product

    def GetVersionString(self):
        return "21.0.4.5"

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project


ALL_EXIST = {"exists": lambda p: True, "drive_type": lambda p: 3, "size_of": lambda p: 1234}


def make_resolve(items_and_comps, settings=None, timeline_settings=None, extra_pool=()):
    media = [FakeMediaItem(p, f"uid{i}") for i, (p, _c, _n) in enumerate(items_and_comps)]
    tl_items = [FakeTimelineItem(m, comps, nodes)
                for m, (_p, comps, nodes) in zip(media, items_and_comps)]
    root = FakeFolder("Master", media[:1], [FakeFolder("Sub", media[1:] + list(extra_pool))])
    return FakeResolve(FakeProject(root, FakeTimeline(tl_items, timeline_settings), settings))


# ---------------------------------------------------------------- parsing
def test_codec_family():
    assert codec_family("H.264 High L4.2") == "long-gop"
    assert codec_family("H.265 Main 10") == "long-gop"
    assert codec_family("Apple ProRes 422 HQ") == "intra"
    assert codec_family("DNxHR HQX") == "intra"
    assert codec_family("") == "unknown"


def test_clip_from_real_property_snapshot():
    clip = clip_from_properties(props(), **ALL_EXIST)
    assert (clip.width, clip.height, clip.fps, clip.bit_depth) == (1920, 1080, 60.0, 8)
    assert clip.frames == 3605 and round(clip.seconds) == 60
    assert clip.long_gop and clip.online and clip.proxy == "None" and clip.usage == 2
    assert clip.location == "local" and clip.size_bytes == 1234


def test_location_classification():
    assert classify_location("", lambda p: True) == "missing"
    assert classify_location(r"D:\x.mp4", lambda p: False) == "missing"
    assert classify_location(r"\\nas\share\x.mp4", lambda p: True, lambda p: 3) == "network"
    assert classify_location(r"C:\Users\me\OneDrive\x.mp4", lambda p: True, lambda p: 3) == "onedrive"
    assert classify_location(r"E:\x.mp4", lambda p: True, lambda p: 2) == "removable"
    assert classify_location(r"Z:\x.mp4", lambda p: True, lambda p: 4) == "network"
    assert classify_location(r"D:\x.mp4", lambda p: True, lambda p: 3) == "local"


# ---------------------------------------------------------------- reading
def test_scan_walks_folders_skips_non_video_and_matches_timeline():
    audio = FakeMediaItem(props(**{"Clip Name": "music.wav", "Type": "Audio"}), "uidA")
    resolve = make_resolve([
        (props(), [FakeComp("MediaIn", "MediaOut", "AudioDisplay")], 1),
        (props(**{"Clip Name": "b.mov", "Video Codec": "Apple ProRes 422"}), [], 6),
    ], extra_pool=[audio])
    report = scan(resolve, platform="win32", **ALL_EXIST)
    assert [c.name for c in report.clips] == ["cam.mp4", "b.mov"]      # audio skipped
    t = report.timeline
    assert (t.name, t.fps, t.width, t.height, t.video_tracks, t.clip_count) == (
        "Timeline 1", 60.0, 1920, 1080, 1, 2)
    assert t.start_frame == 216000
    assert [(i["name"], i["track"], i["start"], i["end"], i["clip"]) for i in t.items] == [
        ("cam.mp4", 1, 216000, 217029, "cam.mp4"), ("b.mov", 1, 216000, 217029, "b.mov")]
    assert all(c.on_timeline for c in report.clips)
    assert report.clips[0].fusion_tools == []
    assert report.clips[1].color_nodes == 6
    assert report.settings.proxy_mode == "2" and report.settings.optimized_media_on


def test_default_fusion_comp_is_not_an_effect_but_real_tools_are():
    resolve = make_resolve([
        (props(), [FakeComp("MediaIn", "Blur", "Glow", "MediaOut")], 1),
    ])
    report = scan(resolve, platform="win32", **ALL_EXIST)
    assert report.clips[0].fusion_tools == ["Blur", "Glow"]
    assert report.timeline.items[0]["fusion_tools"] == ["Blur", "Glow"]
    by_code = {f.code: f for f in report.findings}
    assert "fusion-comp" in by_code and "render-cache-off" in by_code
    assert by_code["fusion-comp"].subject == "cam.mp4 @V1 01:00:00:00"


def test_scan_without_timeline():
    project = FakeProject(FakeFolder("Master", [FakeMediaItem(props(), "u")]), timeline=None)
    report = scan(FakeResolve(project), platform="win32", **ALL_EXIST)
    assert report.timeline is None and len(report.clips) == 1
    assert not report.clips[0].on_timeline


# ------------------------------------------------------------------ rules
def clip(**over):
    return clip_from_properties(props(**over), **ALL_EXIST)


def settings(**over):
    base = dict(proxy_mode="2", proxy_resolution="original", render_cache_mode="none",
                render_cache_codec="dnxhd", optimized_media_on=True, optimized_codec="dnxhd",
                super_scale=1)
    base.update(over)
    return ProjectSettings(**base)


TL = TimelineInfo("Timeline 1", 60.0, 1920, 1080, 1, 1)


def codes(findings):
    return [f.code for f in findings]


def but_codec(findings):
    """Everything except the long-GOP note, which every H.264 clip gets on every edition."""
    return [f for f in findings if f.code != "codec-long-gop"]


def test_long_gop_is_high_when_heavy_medium_otherwise():
    light = clip(**{"FPS": 24.0})
    heavy = clip(**{"Video Codec": "H.265 Main 10", "Bit Depth": "10", "Resolution": "3840x2160"})
    f_light = find_issues([light], TL, settings(), platform="win32")
    f_heavy = find_issues([heavy], TL, settings(), platform="win32")
    assert f_light[0].code == "codec-long-gop" and f_light[0].severity == "medium"
    assert f_heavy[0].code == "codec-long-gop" and f_heavy[0].severity == "high"
    assert "10-bit" in f_heavy[0].message and "3840x2160" in f_heavy[0].message and "H.265" in f_heavy[0].message


def test_long_gop_is_only_noted_where_the_gpu_decodes_it():
    for found in (find_issues([clip()], TL, settings(), platform="darwin"),
                  find_issues([clip()], TL, settings(), platform="win32", edition="studio")):
        assert codes(found) == ["codec-long-gop"] and found[0].severity == "info"
        assert found[0].message == "H.264 High L4.2 is long-GOP" and "GPU" in found[0].why
    prores = clip(**{"Video Codec": "Apple ProRes 422 HQ"})
    assert find_issues([prores], TL, settings(), platform="win32") == []


def test_scan_reads_the_edition_and_shows_it():
    resolve = make_resolve([(props(), [], 1)])
    resolve.product = "DaVinci Resolve Studio"
    report = scan(resolve, platform="win32", **ALL_EXIST)
    assert (report.edition, report.version) == ("studio", "21.0.4.5")
    assert report.inventory_text().startswith("resolve  : Studio 21.0.4.5\n")
    assert [f.severity for f in report.findings if f.code == "codec-long-gop"] == ["info"]
    assert report.to_dict()["edition"] == "studio"
    resolve.product = "DaVinci Resolve"
    assert scan(resolve, platform="win32", **ALL_EXIST).edition == "free"


def test_missing_media_is_high_and_suppresses_other_clip_findings():
    missing = clip_from_properties(props(), exists=lambda p: False)
    found = find_issues([missing], TL, settings(), platform="win32")
    assert codes(found) == ["media-missing"] and found[0].severity == "high"
    offline = clip(**{"Online Status": "Offline"})
    assert codes(find_issues([offline], TL, settings(), platform="win32")) == ["media-missing"]


def test_location_findings():
    one = clip_from_properties(props(**{"File Path": r"C:\Users\me\OneDrive\a.mp4"}),
                               exists=lambda p: True, drive_type=lambda p: 3)
    net = clip_from_properties(props(**{"File Path": r"\\nas\a.mp4"}), exists=lambda p: True)
    usb = clip_from_properties(props(), exists=lambda p: True, drive_type=lambda p: 2)
    assert "media-onedrive" in codes(find_issues([one], TL, settings(), platform="darwin"))
    assert "media-network" in codes(find_issues([net], TL, settings(), platform="darwin"))
    assert "media-removable" in codes(find_issues([usb], TL, settings(), platform="darwin"))


def test_timeline_mismatch_findings_only_for_clips_on_the_timeline():
    c = clip(**{"Resolution": "3840x2160", "FPS": 23.976})
    assert but_codec(find_issues([c], TL, settings(), platform="darwin")) == []
    c.on_timeline = True
    found = codes(find_issues([c], TL, settings(), platform="darwin"))
    assert "res-above-timeline" in found and "fps-mismatch" in found


def test_super_scale_findings():
    c = clip(**{"Super Scale": 2})
    assert "super-scale" in codes(find_issues([c], TL, settings(), platform="darwin"))
    assert "project-super-scale" in codes(find_issues([], TL, settings(super_scale=3)))


def test_proxy_mode_findings_only_when_proxies_exist():
    with_proxy = clip(**{"Proxy": "Half", "Proxy Media Path": r"D:\proxies\cam.mov"})
    assert "proxy-mode-originals" in codes(find_issues([with_proxy], TL, settings(proxy_mode="2"), "darwin"))
    assert "proxy-mode-disabled" in codes(find_issues([with_proxy], TL, settings(proxy_mode="0"), "darwin"))
    assert but_codec(find_issues([with_proxy], TL, settings(proxy_mode="1"), "darwin")) == []
    assert but_codec(find_issues([clip()], TL, settings(proxy_mode="2"), "darwin")) == []


def test_deep_grade_and_render_cache_hint_are_per_timeline_item():
    c = clip()
    c.on_timeline = True
    tl = TimelineInfo("Timeline 1", 60.0, 1920, 1080, 1, 2, 216000, items=[
        {"name": "cam.mp4", "track": 1, "start": 216000, "end": 216600, "clip": "cam.mp4",
         "label": "cam.mp4 @V1 01:00:00:00", "fusion_tools": [], "color_nodes": 7},
        {"name": "cam.mp4", "track": 1, "start": 216600, "end": 217200, "clip": "cam.mp4",
         "label": "cam.mp4 @V1 01:00:10:00", "fusion_tools": ["Blur"], "color_nodes": 1}])
    found = but_codec(find_issues([c], tl, settings(render_cache_mode="none"), "darwin"))
    assert [(f.code, f.subject) for f in found] == [
        ("fusion-comp", "cam.mp4 @V1 01:00:10:00"),
        ("deep-grade", "cam.mp4 @V1 01:00:00:00"),
        ("render-cache-off", "project")]
    assert "2 timeline clip(s)" in found[2].message
    assert "render-cache-off" not in codes(find_issues([c], tl, settings(render_cache_mode="smart"), "darwin"))


def test_item_labels_are_unique_per_position():
    from renderflow.scan import frames_to_timecode, item_label
    assert frames_to_timecode(216000, 60.0) == "01:00:00:00"
    assert frames_to_timecode(216000 + 17 * 60 + 8, 60.0) == "01:00:17:08"
    assert item_label("cam.mp4", 2, 216000, 60.0) == "cam.mp4 @V2 01:00:00:00"


def test_findings_sorted_by_severity():
    c = clip(**{"Super Scale": 2})
    c.on_timeline, c.color_nodes = True, 9
    found = find_issues([c], TL, settings(), platform="win32")
    order = [f.severity for f in found]
    assert order == sorted(order, key={"high": 0, "medium": 1, "info": 2}.get)


# ----------------------------------------------------------------- output
def test_text_and_json_reports():
    resolve = make_resolve([(props(), [FakeComp("MediaIn", "Blur", "MediaOut")], 1)])
    report = scan(resolve, platform="win32", **ALL_EXIST)
    text = report.text()
    assert "cam.mp4" in text and "fusion(1)" in text and "--- high" in text
    assert "prefer originals" in text
    data = json.loads(json.dumps(report.to_dict()))
    assert data["project"] == "wowo"
    assert data["clips"][0]["codec_family"] == "long-gop"
    assert data["clips"][0]["fusion_tools"] == ["Blur"]
    assert {f["code"] for f in data["findings"]} >= {"codec-long-gop", "fusion-comp"}


def test_findings_text_groups_what_is_said_about_several_subjects():
    from renderflow.scan import Finding, findings_text
    same = [Finding("high", "codec-long-gop", f"cam{k}.mp4", "H.264 in software", "why gop")
            for k in range(6)]
    one = Finding("high", "super-scale", "cam0.mp4", "Super Scale 2x", "why scale")
    other = Finding("medium", "codec-long-gop", "old.mp4", "H.264 in software", "why gop")
    text = findings_text(same + [one, other], "none")
    assert text.splitlines() == [
        "--- high (7) ---",
        "[high] 6 clips: H.264 in software",
        "         cam0.mp4, cam1.mp4, cam2.mp4, +3 more",
        "         why gop",
        "[high] cam0.mp4: Super Scale 2x",
        "         why scale",
        "--- medium (1) ---",
        "[medium] old.mp4: H.264 in software",          # same code, already explained above
    ]


def test_inventory_res_column_lines_up():
    from renderflow.scan import ScanReport, TimelineInfo
    small = clip(**{"Clip Name": "sd.mp4", "Resolution": "848x480"})
    rep = ScanReport("p", "win32", TimelineInfo("t", 30.0, 1920, 1080, 1, 0), settings(), [clip(), small])
    rows = rep.inventory_text().splitlines()[-2:]
    assert rows[0].index(" 1920x1080 ") == rows[1].index("   848x480 ")


def test_text_report_with_no_findings():
    report = scan(make_resolve([(props(**{"Video Codec": "DNxHR HQ"}), [], 1)]),
                  platform="win32", **ALL_EXIST)
    assert "no findings" in report.text()
