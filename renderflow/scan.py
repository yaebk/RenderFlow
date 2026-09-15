"""Inventory a Resolve project and point at what is likely slowing it down.

This is the *unmeasured* half of the profiler: it reads facts Resolve already
knows (codec, resolution, bit depth, file location, proxies, effects, project
settings) and applies explainable rules of thumb to them. Every finding says
why it was raised. The measured half - timing decode and render on this
machine - comes separately and can override these guesses.

    from renderflow import connect, scan
    report = scan(connect())
    print(report.text())
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# Codecs Resolve decodes in software on the free edition (Windows/Linux):
# long-GOP camera and screen-capture formats. Every frame depends on the frames
# around it, so scrubbing and reverse playback are expensive.
LONG_GOP = ("H.264", "H.265", "HEVC", "AV1", "VP9", "MPEG-4", "MPEG-2", "MPEG", "XAVC", "AVC")
INTRA = ("ProRes", "DNx", "CineForm", "MJPEG", "Motion JPEG", "Uncompressed", "BRAW",
         "R3D", "ARRIRAW", "Photo JPEG", "PNG", "TIFF", "EXR", "DPX", "JPEG 2000", "GoPro CineForm")

VIDEO_TYPES = ("Video", "Video + Audio")

# Every clip has a default Fusion comp made of these; only other tools mean work.
FUSION_PASSTHROUGH = {"MediaIn", "MediaOut", "AudioDisplay", "Loader", "Saver"}

SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


def sort_findings(findings: "list[Finding]") -> "list[Finding]":
    """Order findings high -> medium -> info, then by subject."""
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))


@dataclass
class ClipInfo:
    name: str
    path: str
    type: str
    codec: str
    format: str
    width: int
    height: int
    fps: float
    bit_depth: int
    frames: int
    online: bool
    proxy: str
    proxy_path: str
    usage: int
    super_scale: int
    start_tc: str = ""
    location: str = "local"         # local | onedrive | network | removable | missing
    size_bytes: int | None = None
    on_timeline: bool = False
    fusion_tools: list[str] = field(default_factory=list)   # real tools, passthrough excluded
    color_nodes: int = 0
    unique_id: str = ""
    measured: dict | None = None    # filled in by renderflow.profile

    @property
    def codec_family(self) -> str:
        return codec_family(self.codec)

    @property
    def long_gop(self) -> bool:
        return self.codec_family == "long-gop"

    @property
    def seconds(self) -> float:
        return self.frames / self.fps if self.fps else 0.0


@dataclass
class TimelineInfo:
    name: str
    fps: float
    width: int
    height: int
    video_tracks: int
    clip_count: int
    start_frame: int = 0
    # one dict per timeline item: name, track, start, end, clip, label, fusion_tools, color_nodes
    items: list[dict] = field(default_factory=list)


@dataclass
class ProjectSettings:
    proxy_mode: str                 # 0 disabled | 1 prefer proxies | 2 prefer originals
    proxy_resolution: str
    render_cache_mode: str          # none | smart | user
    render_cache_codec: str
    optimized_media_on: bool
    optimized_codec: str
    super_scale: int


@dataclass
class Finding:
    severity: str                   # high | medium | info
    code: str
    subject: str                    # clip name, timeline item label, or "project"
    message: str
    why: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.subject}: {self.message}\n         {self.why}"


@dataclass
class ScanReport:
    project: str
    platform: str
    timeline: TimelineInfo | None
    settings: ProjectSettings
    clips: list[ClipInfo]
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for clip, raw in zip(self.clips, data["clips"]):
            raw["codec_family"] = clip.codec_family
            raw["seconds"] = round(clip.seconds, 2)
        return data

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def inventory_text(self) -> str:
        """Header, settings and the clip table - everything except the findings."""
        lines = [f"project  : {self.project}"]
        if self.timeline:
            t = self.timeline
            lines.append(f"timeline : {t.name}  {t.width}x{t.height} @ {t.fps:g} fps, "
                         f"{t.clip_count} clips on {t.video_tracks} video track(s)")
        s = self.settings
        proxy_words = {"0": "disabled", "1": "prefer proxies", "2": "prefer originals"}
        lines.append(f"settings : proxies={proxy_words.get(s.proxy_mode, s.proxy_mode)}  "
                     f"render cache={s.render_cache_mode}  "
                     f"optimized media={'on' if s.optimized_media_on else 'off'}")
        lines.append("")
        lines.append(f"{'clip':<34} {'codec':<18} {'res':>9} {'fps':>5} {'bit':>3} "
                     f"{'secs':>6} {'where':<9} {'proxy':<6} fx")
        for c in self.clips:
            fx = []
            if c.fusion_tools:
                fx.append(f"fusion({len(c.fusion_tools)})")
            if c.color_nodes > 1:
                fx.append(f"{c.color_nodes} nodes")
            if c.super_scale > 1:
                fx.append(f"superscale {c.super_scale}x")
            name = (c.name[:31] + "...") if len(c.name) > 34 else c.name
            lines.append(f"{name:<34} {c.codec[:18]:<18} {c.width}x{c.height:>4} {c.fps:>5g} "
                         f"{c.bit_depth:>3} {c.seconds:>6.0f} {c.location:<9} {c.proxy[:6]:<6} "
                         f"{', '.join(fx)}")
        return "\n".join(lines)

    def findings_text(self) -> str:
        return findings_text(self.findings, "no findings - nothing here looks like a bottleneck.")

    def text(self) -> str:
        return self.inventory_text() + "\n\n" + self.findings_text()


def findings_text(findings: list[Finding], empty: str) -> str:
    """Findings grouped by severity, or ``empty`` when there are none."""
    if not findings:
        return empty
    lines = []
    for severity in SEVERITY_ORDER:
        group = [f for f in findings if f.severity == severity]
        if group:
            lines.append(f"--- {severity} ({len(group)}) ---")
            lines.extend(str(f) for f in group)
    return "\n".join(lines)


# ------------------------------------------------------------------ parsing
def codec_family(codec: str) -> str:
    upper = codec.upper()
    if any(k.upper() in upper for k in INTRA):
        return "intra"
    if any(k.upper() in upper for k in LONG_GOP):
        return "long-gop"
    return "other" if codec else "unknown"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolution(value: Any) -> tuple[int, int]:
    match = re.match(r"\s*(\d+)\s*x\s*(\d+)", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def classify_location(path: str, exists: Callable[[str], bool] = os.path.exists,
                      drive_type: Callable[[str], int | None] | None = None) -> str:
    if not path:
        return "missing"
    if not exists(path):
        return "missing"
    if path.startswith(("\\\\", "//")):
        return "network"
    if "onedrive" in path.lower():
        return "onedrive"
    drive_type = drive_type or windows_drive_type
    kind = drive_type(path)
    if kind == 2:
        return "removable"
    if kind == 4:
        return "network"
    return "local"


def windows_drive_type(path: str) -> int | None:
    """2 removable, 3 fixed, 4 remote, 5 cdrom, 6 ramdisk; None off Windows."""
    if sys.platform != "win32":
        return None
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return None
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))  # type: ignore[attr-defined]
    except Exception:
        return None


def clip_from_properties(props: dict[str, Any], exists=os.path.exists, drive_type=None,
                         size_of: Callable[[str], int | None] | None = None) -> ClipInfo:
    path = str(props.get("File Path") or "")
    width, height = _resolution(props.get("Resolution"))
    location = classify_location(path, exists, drive_type)
    size = None
    if location != "missing":
        try:
            size = (size_of or os.path.getsize)(path)
        except OSError:
            size = None
    return ClipInfo(
        name=str(props.get("Clip Name") or props.get("File Name") or ""),
        path=path,
        type=str(props.get("Type") or ""),
        codec=str(props.get("Video Codec") or ""),
        format=str(props.get("Format") or ""),
        width=width,
        height=height,
        fps=_float(props.get("FPS")),
        bit_depth=_int(props.get("Bit Depth"), 8),
        frames=_int(props.get("Frames")),
        online=str(props.get("Online Status") or "Online") == "Online",
        proxy=str(props.get("Proxy") or "None"),
        proxy_path=str(props.get("Proxy Media Path") or ""),
        usage=_int(props.get("Usage")),
        super_scale=_int(props.get("Super Scale"), 1),
        start_tc=str(props.get("Start TC") or ""),
        location=location,
        size_bytes=size,
    )


# ------------------------------------------------------------------- reading
def _walk_folders(folder):
    yield folder
    for sub in folder.GetSubFolderList() or []:
        yield from _walk_folders(sub)


def read_clips(project, **kw) -> list[ClipInfo]:
    """Every video clip in the media pool, all folders, with its properties."""
    clips = []
    root = project.GetMediaPool().GetRootFolder()
    for folder in _walk_folders(root):
        for item in folder.GetClipList() or []:
            props = item.GetClipProperty() or {}
            if str(props.get("Type") or "") not in VIDEO_TYPES:
                continue
            clip = clip_from_properties(props, **kw)
            try:
                clip.unique_id = str(item.GetUniqueId() or "")
            except Exception:
                clip.unique_id = ""
            clips.append(clip)
    return clips


def frames_to_timecode(frame: int, fps: float) -> str:
    rate = max(1, int(round(fps)))
    frames = frame % rate
    seconds = frame // rate
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}:{frames:02d}"


def item_label(name: str, track: int, start: int, fps: float) -> str:
    """Unique, readable identity for a timeline item: the same clip can be on
    the timeline many times, so the name alone is not enough."""
    return f"{name} @V{track} {frames_to_timecode(start, fps)}"


def read_timeline(project, clips: list[ClipInfo]) -> TimelineInfo | None:
    """Current timeline summary; also marks which clips are on it and their effects."""
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        return None
    by_id = {c.unique_id: c for c in clips if c.unique_id}
    by_path = {c.path: c for c in clips if c.path}
    tracks = _int(timeline.GetTrackCount("video"))
    fps = _float(timeline.GetSetting("timelineFrameRate"))
    count = 0
    items: list[dict] = []
    for index in range(1, tracks + 1):
        for item in timeline.GetItemListInTrack("video", index) or []:
            count += 1
            clip = _match_clip(item, by_id, by_path)
            tools = _fusion_tools(item)
            nodes = _node_count(item)
            name, start = str(item.GetName()), _int(item.GetStart())
            items.append({"name": name, "track": index, "start": start,
                          "end": _int(item.GetEnd()), "clip": clip.name if clip else "",
                          "label": item_label(name, index, start, fps),
                          "fusion_tools": tools, "color_nodes": nodes})
            if clip is None:
                continue
            # The clip table shows each source clip's heaviest use on the timeline.
            clip.on_timeline = True
            if len(tools) > len(clip.fusion_tools):
                clip.fusion_tools = tools
            clip.color_nodes = max(clip.color_nodes, nodes)
    return TimelineInfo(
        name=str(timeline.GetName()),
        fps=fps,
        width=_int(timeline.GetSetting("timelineResolutionWidth")),
        height=_int(timeline.GetSetting("timelineResolutionHeight")),
        video_tracks=tracks,
        clip_count=count,
        start_frame=_safe_int(lambda: timeline.GetStartFrame()),
        items=items,
    )


def _match_clip(item, by_id, by_path):
    try:
        media = item.GetMediaPoolItem()
    except Exception:
        return None
    if media is None:
        return None
    try:
        uid = str(media.GetUniqueId() or "")
        if uid in by_id:
            return by_id[uid]
    except Exception:
        pass
    try:
        return by_path.get(str(media.GetClipProperty("File Path") or ""))
    except Exception:
        return None


def _safe_int(fn) -> int:
    try:
        return _int(fn())
    except Exception:
        return 0


def _fusion_tools(item) -> list[str]:
    """Tool types in the clip's Fusion comps, ignoring the default passthrough."""
    found: list[str] = []
    try:
        for index in range(1, _safe_int(item.GetFusionCompCount) + 1):
            comp = item.GetFusionCompByIndex(index)
            if comp is None:
                continue
            for tool in (comp.GetToolList() or {}).values():
                reg_id = str((tool.GetAttrs() or {}).get("TOOLS_RegID") or "")
                if reg_id and reg_id not in FUSION_PASSTHROUGH:
                    found.append(reg_id)
    except Exception:
        pass
    return found


def _node_count(item) -> int:
    try:
        graph = item.GetNodeGraph()
        return _int(graph.GetNumNodes()) if graph is not None else 0
    except Exception:
        return 0


def read_settings(project) -> ProjectSettings:
    get = project.GetSetting
    return ProjectSettings(
        proxy_mode=str(get("perfProxyMediaMode") or ""),
        proxy_resolution=str(get("perfProxyResolutionRatio") or ""),
        render_cache_mode=str(get("perfRenderCacheMode") or ""),
        render_cache_codec=str(get("perfRenderCacheCodec") or ""),
        optimized_media_on=str(get("perfOptimisedMediaOn") or "0") == "1",
        optimized_codec=str(get("perfOptimisedCodec") or ""),
        super_scale=_int(get("superScale"), 1),
    )


# -------------------------------------------------------------------- rules
def find_issues(clips: list[ClipInfo], timeline: TimelineInfo | None,
                settings: ProjectSettings, platform: str = sys.platform) -> list[Finding]:
    out: list[Finding] = []
    software_decode = platform != "darwin"      # macOS free edition hardware-decodes H.264/H.265

    for c in clips:
        if c.location == "missing" or not c.online:
            out.append(Finding("high", "media-missing", c.name,
                               "file is offline or missing",
                               f"Resolve cannot read {c.path or '(no path)'}; the clip shows as "
                               "offline media and anything using it cannot play."))
            continue

        if c.long_gop and software_decode:
            heavy_parts = _heavy_decode_parts(c)
            why = ("Long-GOP codecs (H.264/H.265/AV1) are the usual cause of stuttering on Windows: "
                   "the free edition does not use the GPU to decode them, and every frame depends on "
                   "its neighbours so scrubbing is worst. A proxy (DNxHR/ProRes) removes this entirely.")
            if heavy_parts:
                why += " This one is heavy: " + ", ".join(heavy_parts) + "."
            out.append(Finding("high" if heavy_parts else "medium", "codec-long-gop", c.name,
                               f"{c.codec} is decoded in software on the free edition", why))

        if c.location == "onedrive":
            out.append(Finding("medium", "media-onedrive", c.name,
                               "source lives inside a OneDrive folder",
                               "OneDrive can dehydrate files to placeholders and lock them while "
                               "syncing; Resolve then stalls or shows offline media. Move footage to "
                               "a plain local folder or set it to Always keep on this device."))
        elif c.location == "network":
            out.append(Finding("medium", "media-network", c.name,
                               "source is on a network location",
                               "Playback is limited by the network, not the machine. Copy the "
                               "footage locally for editing."))
        elif c.location == "removable":
            out.append(Finding("medium", "media-removable", c.name,
                               "source is on a removable drive",
                               "USB and SD media are often too slow for smooth playback of "
                               "high-bitrate footage. Copy it to an internal drive."))

        if timeline and c.on_timeline:
            if timeline.width and c.width > timeline.width * 1.5:
                out.append(Finding("info", "res-above-timeline", c.name,
                                   f"{c.width}x{c.height} source on a {timeline.width}x{timeline.height} timeline",
                                   "Every frame is decoded at full size then scaled down. Not a "
                                   "problem by itself, but a proxy at timeline resolution makes "
                                   "this clip both cheaper to decode and cheaper to scale."))
            if timeline.fps and c.fps and abs(c.fps - timeline.fps) > 0.01:
                out.append(Finding("info", "fps-mismatch", c.name,
                                   f"{c.fps:g} fps source on a {timeline.fps:g} fps timeline",
                                   "Resolve retimes it on the fly. Usually cheap, but combined with "
                                   "optical-flow retiming it is not."))

        if c.super_scale > 1:
            out.append(Finding("high", "super-scale", c.name,
                               f"Super Scale {c.super_scale}x is enabled on this clip",
                               "Super Scale upscales every frame with a neural filter - one of the "
                               "slowest things Resolve can do in real time. Turn it off while "
                               "editing and re-enable it for the final render."))

    # Effects live on timeline items, not source clips: the same clip can be
    # on the timeline several times with different work on each copy.
    for item in (timeline.items if timeline else []):
        tools, nodes = item.get("fusion_tools") or [], item.get("color_nodes") or 0
        if tools:
            kinds = sorted(set(tools))
            out.append(Finding("medium", "fusion-comp", item["label"],
                               f"Fusion composition with {len(tools)} tool(s): "
                               + ", ".join(kinds[:6]) + (", ..." if len(kinds) > 6 else ""),
                               "Fusion comps are rendered per frame during playback. Render Cache "
                               "(Smart) or render-in-place turns them into plain video."))
        if nodes > 4:
            out.append(Finding("info", "deep-grade", item["label"],
                               f"{nodes}-node colour grade",
                               "Each node is a pass over every frame; noise reduction or blur "
                               "nodes dominate. Enable Render Cache if this clip stutters."))

    with_proxy = [c for c in clips if c.proxy not in ("", "None")]
    if settings.proxy_mode == "2" and with_proxy:
        out.append(Finding("high", "proxy-mode-originals", "project",
                           f"{len(with_proxy)} clip(s) have proxies but Playback is set to "
                           "Prefer Camera Originals",
                           "Resolve ignores the proxies unless the originals are unavailable, so "
                           "the work of making them is wasted. Playback -> Proxy Handling -> "
                           "Prefer Proxies (perfProxyMediaMode = 1)."))
    elif settings.proxy_mode == "0" and with_proxy:
        out.append(Finding("high", "proxy-mode-disabled", "project",
                           f"{len(with_proxy)} clip(s) have proxies but proxies are disabled",
                           "Playback -> Proxy Handling -> Prefer Proxies."))

    if settings.super_scale > 1:
        out.append(Finding("high", "project-super-scale", "project",
                           f"project-wide Super Scale is {settings.super_scale}x",
                           "Applies neural upscaling to everything. Disable while editing."))

    heavy_fx = [i for i in (timeline.items if timeline else [])
                if i.get("fusion_tools") or (i.get("color_nodes") or 0) > 4]
    if settings.render_cache_mode == "none" and heavy_fx:
        out.append(Finding("info", "render-cache-off", "project",
                           f"Render Cache is off and {len(heavy_fx)} timeline clip(s) carry effects",
                           "Playback -> Render Cache -> Smart caches effect-heavy clips in the "
                           "background so they play without re-rendering."))

    return sort_findings(out)


def _heavy_decode_parts(c: ClipInfo) -> list[str]:
    """What makes a long-GOP clip expensive to decode, as short phrases."""
    parts = []
    if c.bit_depth >= 10:
        parts.append(f"{c.bit_depth}-bit")
    if c.width >= 3840:
        parts.append(f"{c.width}x{c.height}")
    if c.fps > 30:
        parts.append(f"{c.fps:g} fps")
    if "265" in c.codec or "HEVC" in c.codec.upper():
        parts.append("H.265")
    return parts


# --------------------------------------------------------------------- entry
def scan(resolve, platform: str = sys.platform, **kw) -> ScanReport:
    """Inventory the open project and return a :class:`ScanReport`."""
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise RuntimeError("no project is open in Resolve")
    clips = read_clips(project, **kw)
    timeline = read_timeline(project, clips)
    settings = read_settings(project)
    report = ScanReport(str(project.GetName()), platform, timeline, settings, clips)
    report.findings = find_issues(clips, timeline, settings, platform)
    return report
