"""Apply the fixes the measurements justify - and undo them.

Four kinds of change, each one reversible and written to a journal as it is
made, so a crash halfway through still leaves an undo trail:

* **proxies** - a DNxHR LB proxy for every clip whose decode measured below
  2x real time (or every long-GOP clip, if asked), linked with
  ``LinkProxyMedia``; and Playback -> Proxy Handling set to *Prefer Proxies*,
  without which Resolve ignores them.
* **settings** - project-wide Super Scale off; Render Cache to Smart when a
  clip on the timeline measured heavy and carries effects; per-clip Super
  Scale off.
* **markers** - a timeline marker over every clip (or measured stretch of
  short clips) with a high or medium finding, coloured by severity, note = the
  finding, so the report is visible inside Resolve. Tagged with custom data so
  undo removes exactly these. Frames that already have a marker are skipped:
  Resolve allows one per frame.
* **tool bypass** - when the tool attribution has run, the Fusion tools it
  measured as the cost of a below-real-time comp are switched to pass-through
  (the node's own bypass button) so the clip plays while editing. Nothing is
  deleted or changed; the effect is simply off until ``fix --restore-tools``
  (or ``--undo``) puts it back, and every report carries a high finding
  while it is off so a delivery is not made without it.

What is deliberately *not* here: render-in-place. Smart Render Cache is
Resolve's own answer for effect-heavy clips on every edition, it does not
freeze content, and it undoes itself. Baking clips to files is a bigger
change than the evidence so far justifies.

    plan    -> list of Actions (nothing touched)
    apply   -> carries them out, journaling each
    undo    -> reverses the journal, newest first
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from renderflow.proxies import (
    DEFAULT_MAX_WIDTH,
    DEFAULT_PROXY_DIR,
    ProxySpec,
    generate_proxy,
    plan_proxy,
)
from renderflow.rendercost import RenderProfile
from renderflow.scan import ClipInfo, Finding, ScanReport, _walk_folders, timeline_named
from renderflow.tools import ToolReport, find_tool, heavy_tools, is_passing, set_passing

JOURNAL_PATH = Path.home() / ".renderflow" / "journal.json"
MARKER_TAG = "renderflow"
ENCODE_SPEED = 4.0                  # proxies encode at roughly this many x real time (measured 5.4x)
MARKER_COLORS = {"high": "Red", "medium": "Yellow"}
MARKED_CODES = {"render-heavy", "render-slow", "decode-below-realtime", "decode-marginal",
                "fusion-comp", "fusion-tool-heavy", "super-scale", "media-missing", "seek-slow"}


@dataclass
class Action:
    kind: str                       # proxy | setting | clip-setting | marker | tool-bypass
    subject: str
    summary: str
    why: str
    params: dict[str, Any] = field(default_factory=dict)
    estimate_s: float = 0.0

    def __str__(self) -> str:
        est = f"  (~{self.estimate_s / 60:.0f} min)" if self.estimate_s >= 90 else (
            f"  (~{self.estimate_s:.0f}s)" if self.estimate_s else "")
        kind = self.kind
        if kind == "marker" and self.params.get("color"):
            kind = f"{self.params['color'].lower()} marker"
        line = f"[{kind}] {self.subject}: {self.summary}{est}"
        return f"{line}\n         {self.why}" if self.why else line


# ------------------------------------------------------------------- plan
def plan(report: ScanReport, render: RenderProfile | None = None, findings: list[Finding] | None = None,
         proxies: str = "auto", settings: bool = True, markers: bool = True,
         proxy_dir: Path | str = DEFAULT_PROXY_DIR, max_width: int = DEFAULT_MAX_WIDTH,
         platform: str | None = None, notes: list[str] | None = None,
         tools: ToolReport | None = None) -> list[Action]:
    """Decide what to change. ``proxies`` is ``auto`` (measured < 2x), ``all`` (every
    long-GOP clip) or ``none``. ``findings`` defaults to the report's own. Things
    worth telling the user that are not actions (findings that could not be
    marked) are appended to ``notes`` when a list is given. With a ``tools``
    report, the tools it measured as the cost of a heavy comp are bypassed."""
    findings = report.findings if findings is None else findings
    platform = platform or report.platform
    actions: list[Action] = []

    # -- proxies ------------------------------------------------------------
    proxy_clips: list[tuple[ClipInfo, str]] = []
    for clip in report.clips:
        if proxies == "none" or clip.location == "missing" or _has_proxy(clip):
            continue
        ratio = (clip.measured or {}).get("realtime_ratio")
        if ratio is not None and ratio < 2.0:
            proxy_clips.append((clip, f"measured decode {ratio:g}x real time"))
        elif proxies == "all" and clip.long_gop and platform != "darwin":
            proxy_clips.append((clip, f"{clip.codec} is long-GOP (not measured)"))
    for clip, reason in proxy_clips:
        spec = plan_proxy(clip, proxy_dir, max_width)
        actions.append(Action(
            "proxy", clip.name,
            f"make a {spec.width}x{spec.height} DNxHR LB proxy and link it",
            f"{reason}. An intra-frame proxy decodes cheaply and seeks instantly, so playback "
            "stops depending on the source codec. Undo unlinks it and deletes the file.",
            {"path": clip.path, "target": spec.target, "width": spec.width, "height": spec.height,
             "timecode": spec.timecode, "seconds": clip.seconds, "source_width": clip.width},
            estimate_s=clip.seconds / ENCODE_SPEED,
        ))
    has_proxies = bool(proxy_clips) or any(_has_proxy(c) for c in report.clips)
    if settings and has_proxies and report.settings.proxy_mode != "1":
        actions.append(Action(
            "setting", "project", "Playback -> Proxy Handling -> Prefer Proxies",
            "Resolve only uses proxies in this mode; in 'Prefer Camera Originals' they are ignored.",
            {"key": "perfProxyMediaMode", "value": "1", "old": report.settings.proxy_mode},
        ))

    # -- settings -----------------------------------------------------------
    if settings and report.settings.super_scale > 1:
        actions.append(Action(
            "setting", "project", "turn project-wide Super Scale off",
            "Neural upscaling of every frame; re-enable for the final render.",
            {"key": "superScale", "value": "1", "old": str(report.settings.super_scale)},
        ))
    if settings:
        for clip in report.clips:
            if clip.super_scale > 1 and clip.path:          # pathless generated media cannot be found again
                actions.append(Action(
                    "clip-setting", clip.name, "turn Super Scale off on this clip",
                    "Neural upscaling of every frame; re-enable for the final render.",
                    {"path": clip.path, "key": "Super Scale", "value": 1, "old": clip.super_scale},
                ))
    heavy_fx = []
    if render is not None:
        for s in render.samples:
            if s.ok and render.ratio(s) < 1.0 and (s.fusion_tools or s.color_nodes > 1):
                heavy_fx.append(s.label)
    if settings and heavy_fx and report.settings.render_cache_mode == "none":
        actions.append(Action(
            "setting", "project", "Playback -> Render Cache -> Smart",
            f"{len(heavy_fx)} clip(s) measured below real time and carry effects "
            f"({', '.join(heavy_fx[:3])}{', ...' if len(heavy_fx) > 3 else ''}). Smart cache "
            "renders them in the background so they play without re-rendering each time.",
            {"key": "perfRenderCacheMode", "value": "smart", "old": report.settings.render_cache_mode},
        ))

    # -- tool bypass --------------------------------------------------------
    # Costliest first, and only as many as it takes to fit the frame budget: the
    # point is playback, not a bare comp.
    for c in (tools.comps if tools else []):
        frame_ms = tools.frame_ms
        chosen: list = []
        saved = 0.0
        for t in heavy_tools(c, frame_ms):
            if c.ms_per_frame - saved <= frame_ms:
                break
            chosen.append(t)
            saved += t.saved_ms_per_frame
        if not chosen:
            continue
        saved = min(saved, c.ms_per_frame)
        left = c.ms_per_frame - saved
        if left <= frame_ms:
            outcome = f"without them the comp should play in real time ({frame_ms:.0f} ms/frame)."
        else:
            stuck = [t for t in c.tools if not t.ok]
            outcome = f"~{left:.0f} ms/frame is left, still over real time ({frame_ms:.0f}): " + (
                ", ".join(_tool_name(t) for t in stuck) + " cannot be bypassed - the comp does not "
                "render without it" if stuck else "the rest is spread over smaller tools"
            ) + ". Smart cache covers what remains."
        actions.append(Action(
            "tool-bypass", c.label + (f" (+{len(c.copies)} more)" if c.copies else ""),
            f"bypass {', '.join(_tool_name(t) for t in chosen)} while editing",
            f"Measured: these cost ~{saved:.0f} of the comp's {c.ms_per_frame:.0f} ms/frame; {outcome} "
            "This is the node's own pass-through switch: nothing is deleted or changed, but the "
            "effect is off in the viewer and in any render until it is put back, and every report "
            "says so in red until then. Re-enable before delivery with  fix --restore-tools  (or "
            "fix --undo).",
            {"timeline": tools.timeline, "comp": c.comp, "tools": [t.name for t in chosen],
             "items": [{"label": c.label, "track": c.track, "start": c.start}] + list(c.copies)},
        ))

    # -- markers ------------------------------------------------------------
    if markers and report.timeline and report.timeline.items:
        stretches = [{"name": s.label, "track": 0, "start": s.start, "end": s.end, "clip": "",
                      "label": s.label} for s in (render.samples if render else []) if s.stretch]
        actions.extend(marker_actions(report, findings, notes, stretches))
    return actions


def _has_proxy(clip: ClipInfo) -> bool:
    return clip.proxy not in ("", "None")


def _tool_name(t) -> str:
    """``Grain1``; but ``donttouch_3_1_2 (BitmapMask)`` when the name does not say what it is."""
    kind = t.kind.rsplit(".", 1)[-1]
    return t.name if kind.lower() in t.name.lower() else f"{t.name} ({kind})"


def marker_actions(report: ScanReport, findings: list[Finding],
                   notes: list[str] | None = None, stretches: list[dict] = ()) -> list[Action]:
    """One marker per timeline frame where an item with a high or medium finding starts.

    Findings are matched by item label first, then by clip name (decode
    findings are per source clip, not per timeline item). ``stretches`` are
    measured runs of short clips, shaped like items (track 0), so a stretch
    finding gets a marker spanning the run. Resolve allows one marker per
    frame on the ruler, so items on different tracks that start together
    share a marker, and frames that already carry a marker - ours from an
    earlier apply, or the editor's own - are left alone and reported in
    ``notes``.
    """
    by_subject: dict[str, list[Finding]] = {}
    for f in findings:
        if f.code in MARKED_CODES and f.severity in ("high", "medium"):
            by_subject.setdefault(f.subject, []).append(f)
    tl = report.timeline
    by_frame: dict[int, list[tuple[dict, list[Finding]]]] = {}
    for item in list(tl.items) + list(stretches):
        hits = (by_subject.get(item.get("label", "")) or by_subject.get(item["name"])
                or by_subject.get(item["clip"]) or [])
        if hits:
            by_frame.setdefault(max(0, item["start"] - tl.start_frame), []).append((item, hits))

    out: list[Action] = []
    ours: list[str] = []
    theirs: list[str] = []
    for frame in sorted(by_frame):
        existing = tl.markers.get(frame)
        if existing is not None:
            label = by_frame[frame][0][0]["label"]
            (ours if existing.startswith(MARKER_TAG) else theirs).append(label)
            continue
        marked = by_frame[frame]
        all_hits = [f for _, hits in marked for f in hits]
        worst = min(all_hits, key=lambda f: 0 if f.severity == "high" else 1)
        first = marked[0][0]
        subject = first["label"] + (f" (+{len(marked) - 1} more)" if len(marked) > 1 else "")
        if len(marked) == 1:
            note = "; ".join(f"{f.code}: {f.message}" for f in marked[0][1][:3])
        else:
            note = "; ".join((f"V{item['track']} " if item["track"] else "")
                             + f"{item['name']}: {hits[0].code}: {hits[0].message}"
                             for item, hits in marked)
        out.append(Action(
            "marker", subject, worst.message, "",
            {"frame": frame, "duration": max(1, max(i["end"] for i, _ in marked) - first["start"]),
             "color": MARKER_COLORS[worst.severity], "name": f"RenderFlow: {worst.code}",
             "note": note[:500], "custom": f"{MARKER_TAG}:{tl.start_frame + frame}",
             "timeline": tl.name},
        ))
    if notes is not None:
        if ours:
            notes.append(f"{len(ours)} finding(s) already marked by an earlier run.")
        if theirs:
            notes.append(f"{len(theirs)} finding(s) not marked - the frame already has a marker of "
                         f"your own: {_some(theirs)}.")
    return out


def _some(labels: list[str], limit: int = 3) -> str:
    return ", ".join(labels[:limit]) + (f", +{len(labels) - limit} more" if len(labels) > limit else "")


def plan_text(actions: list[Action], notes: list[str] = ()) -> str:
    if not actions:
        lines = ["nothing to fix - the measurements do not justify any change."]
    else:
        total = sum(a.estimate_s for a in actions)
        lines = [f"{len(actions)} change(s) planned" + (f", about {total / 60:.0f} min of encoding"
                                                        if total >= 60 else "") + ":"]
        lines.extend(str(a) for a in actions)
        if any(a.kind == "marker" for a in actions):
            lines.append("         markers go on the timeline ruler, red = high, yellow = medium; "
                         "undo removes them.")
    lines.extend(f"         {n}" for n in notes)
    return "\n".join(lines)


# ---------------------------------------------------------------- journal
class Journal:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else JOURNAL_PATH        # looked up at call time (tests move it)
        self.entries: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text("utf-8")).get("entries", [])
            except (OSError, ValueError):
                self.entries = []

    def add(self, entry: dict[str, Any]) -> None:
        entry["at"] = time.time()
        self.entries.append(entry)
        self.save()

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"entries": self.entries}, indent=1), "utf-8")

    def __len__(self) -> int:
        return len(self.entries)


# ------------------------------------------------------------------ apply
def _media_item_by_path(project, path: str):
    if not path:
        return None
    root = project.GetMediaPool().GetRootFolder()
    for folder in _walk_folders(root):
        for item in folder.GetClipList() or []:
            if str(item.GetClipProperty("File Path") or "") == path:
                return item
    return None


def _marker_refusal(timeline, frame: int) -> str:
    """Why Resolve refused AddMarker: usually another marker on that frame."""
    try:
        existing = (timeline.GetMarkers() or {}).get(frame)
    except Exception:
        existing = None
    if existing:
        return f"AddMarker refused: frame {frame} already has marker {existing.get('name')!r}"
    return "AddMarker refused"


def apply(resolve, actions: list[Action], journal: Journal | None = None,
          progress: Callable[[str], None] | None = None, ffmpeg: str | None = None,
          proxy_runner=None) -> list[str]:
    """Carry out ``actions`` in order. Returns the problems encountered (empty = clean)."""
    journal = journal if journal is not None else Journal()
    project = resolve.GetProjectManager().GetCurrentProject()
    problems: list[str] = []
    say = progress or (lambda _m: None)
    marked = 0
    name = project_name(project)

    def record(entry: dict[str, Any]) -> None:
        journal.add({**entry, "project": name})     # so undo knows which project it belongs to

    for action in actions:
        p = action.params
        try:
            if action.kind == "proxy":
                spec = ProxySpec(p["path"], p["target"], p["width"], p["height"], p["timecode"], p["seconds"])
                kwargs = {"runner": proxy_runner} if proxy_runner else {}
                generate_proxy(spec, action.subject, p["source_width"], exe=ffmpeg, progress=say, **kwargs)
                item = _media_item_by_path(project, p["path"])
                if item is None:
                    raise RuntimeError("clip no longer in the media pool")
                if not item.LinkProxyMedia(p["target"]):
                    raise RuntimeError("Resolve refused LinkProxyMedia")
                record({"kind": "proxy", "subject": action.subject, "path": p["path"],
                        "target": p["target"]})
                say(f"  linked proxy for {action.subject}")

            elif action.kind == "setting":
                old = project.GetSetting(p["key"])
                if not project.SetSetting(p["key"], p["value"]):
                    raise RuntimeError(f"SetSetting({p['key']}) refused")
                record({"kind": "setting", "subject": "project", "key": p["key"], "old": old,
                        "new": p["value"]})
                say(f"  {p['key']}: {old} -> {p['value']}")

            elif action.kind == "clip-setting":
                item = _media_item_by_path(project, p["path"])
                if item is None:
                    raise RuntimeError("clip no longer in the media pool")
                old = item.GetClipProperty(p["key"])
                if not item.SetClipProperty(p["key"], p["value"]):
                    raise RuntimeError(f"SetClipProperty({p['key']}) refused")
                record({"kind": "clip-setting", "subject": action.subject, "path": p["path"],
                        "key": p["key"], "old": old, "new": p["value"]})
                say(f"  {action.subject}: {p['key']} {old} -> {p['value']}")

            elif action.kind == "marker":
                timeline = timeline_named(project, p["timeline"])
                if timeline is None:
                    raise RuntimeError(f"timeline {p['timeline']!r} not found")
                if not timeline.AddMarker(p["frame"], p["color"], p["name"], p["note"],
                                          p["duration"], p["custom"]):
                    raise RuntimeError(_marker_refusal(timeline, p["frame"]))
                record({"kind": "marker", "subject": action.subject, "custom": p["custom"],
                        "timeline": p["timeline"]})
                marked += 1

            elif action.kind == "tool-bypass":
                timeline = timeline_named(project, p["timeline"])
                if timeline is None:
                    raise RuntimeError(f"timeline {p['timeline']!r} not found")
                done = by_hand = 0
                for it in p["items"]:
                    for tool_name in p["tools"]:
                        entry = {"timeline": p["timeline"], "track": it["track"], "start": it["start"],
                                 "comp": p["comp"], "tool": tool_name}
                        tool = find_tool(timeline, entry)
                        if tool is None:
                            problems.append(f"tool-bypass {it['label']}: {tool_name} is not in the comp")
                        elif is_passing(tool):
                            by_hand += 1                # bypassed by the editor: not ours to re-enable
                        elif not set_passing(tool, True):
                            problems.append(f"tool-bypass {it['label']}: Resolve refused to bypass {tool_name}")
                        else:
                            record({"kind": "tool-bypass", "subject": it["label"], **entry})
                            done += 1
                say(f"  bypassed {done} Fusion tool(s) on {len(p['items'])} clip(s)"
                    + (f" ({by_hand} already bypassed by hand)" if by_hand else ""))
            else:
                raise RuntimeError(f"unknown action kind {action.kind}")
        except Exception as exc:
            problems.append(f"{action.kind} {action.subject}: {exc}")
            say(f"  FAILED {action.kind} {action.subject}: {exc}")
    if marked:
        say(f"  added {marked} marker(s) to the timeline")
    return problems


def undo(resolve, journal: Journal | None = None, progress: Callable[[str], None] | None = None,
         kinds: set[str] | None = None) -> list[str]:
    """Reverse every journaled change, newest first. Returns problems (empty = clean).

    Only changes made to the project that is open now are reversed; the rest
    stay in the journal until that project is opened again. A setting that
    was changed again by hand since we set it is left as it is. Afterwards any
    RenderFlow marker still on the current timeline is removed too: every one
    carries our tag, so they are ours even if the journal that recorded them
    is gone. With ``kinds``, only entries of those kinds are reversed and the
    rest of the journal is kept (``fix --restore-tools``).
    """
    journal = journal if journal is not None else Journal()
    project = resolve.GetProjectManager().GetCurrentProject()
    problems: list[str] = []
    say = progress or (lambda _m: None)
    remaining: list[dict[str, Any]] = []
    removed = gone = reenabled = by_hand = 0
    name = project_name(project)
    elsewhere: dict[str, int] = {}

    for entry in reversed(journal.entries):
        owner = entry.get("project")
        if kinds is not None and entry.get("kind") not in kinds:
            remaining.append(entry)
            continue
        if owner and owner != name:
            elsewhere[owner] = elsewhere.get(owner, 0) + 1
            remaining.append(entry)
            continue
        try:
            kind = entry["kind"]
            if kind == "proxy":
                item = _media_item_by_path(project, entry["path"])
                if item is not None:
                    item.UnlinkProxyMedia()
                try:
                    os.remove(entry["target"])
                except OSError:
                    pass
                say(f"  unlinked and removed proxy for {entry['subject']}")
            elif kind == "setting":
                current = project.GetSetting(entry["key"])
                if _changed_since(entry, current):
                    say(f"  {entry['key']} left at {current} - changed by hand since")
                else:
                    project.SetSetting(entry["key"], entry["old"])
                    say(f"  {entry['key']} restored to {entry['old']}")
            elif kind == "clip-setting":
                item = _media_item_by_path(project, entry["path"])
                if item is None:
                    raise RuntimeError("clip no longer in the media pool")
                current = item.GetClipProperty(entry["key"])
                if _changed_since(entry, current):
                    say(f"  {entry['subject']}: {entry['key']} left at {current} - changed by hand since")
                else:
                    item.SetClipProperty(entry["key"], entry["old"])
                    say(f"  {entry['subject']}: {entry['key']} restored to {entry['old']}")
            elif kind == "marker":
                timeline = timeline_named(project, entry["timeline"])
                if timeline is None:
                    raise RuntimeError(f"timeline {entry['timeline']!r} not found")
                if timeline.DeleteMarkerByCustomData(entry["custom"]):
                    removed += 1
                else:
                    gone += 1
            elif kind == "tool-bypass":
                timeline = timeline_named(project, entry["timeline"])
                if timeline is None:
                    raise RuntimeError(f"timeline {entry['timeline']!r} not found")
                tool = find_tool(timeline, entry)
                if tool is None:
                    # The clip moved or the comp changed: we cannot find it, so we cannot
                    # leave it in the journal either - say so loudly instead.
                    problems.append(f"tool-bypass {entry['subject']}: {entry['tool']} not found - the "
                                    "clip moved or the comp changed since; if it is still bypassed, "
                                    "re-enable it in Fusion by hand")
                elif not is_passing(tool):
                    by_hand += 1                        # re-enabled by the editor since
                elif set_passing(tool, False):
                    reenabled += 1
                else:
                    raise RuntimeError(f"Resolve refused to re-enable {entry['tool']}")
        except Exception as exc:
            problems.append(f"{entry.get('kind')} {entry.get('subject')}: {exc}")
            remaining.append(entry)
    if removed or gone:
        say(f"  removed {removed} marker(s)" + (f" ({gone} already deleted by hand)" if gone else ""))
    if reenabled or by_hand:
        say(f"  re-enabled {reenabled} Fusion tool(s)"
            + (f" ({by_hand} already re-enabled by hand)" if by_hand else ""))
    for owner, count in elsewhere.items():
        problems.append(f"{count} change(s) were made to project {owner!r}, not {name!r} - "
                        "open that project and run --undo again")
    journal.entries = list(reversed(remaining))
    journal.save()
    if kinds is None:
        swept = sweep_markers(project)
        if swept:
            say(f"  removed {swept} leftover RenderFlow marker(s) the journal did not know about")
    return problems


def bypassed_findings(project: str, journal: Journal | None = None) -> list[Finding]:
    """A high finding per clip whose Fusion tools an earlier apply left bypassed,
    so no report - and no delivery - goes out without noticing."""
    journal = journal if journal is not None else Journal()
    by_item: dict[str, list[str]] = {}
    for e in journal.entries:
        if e.get("kind") == "tool-bypass" and e.get("project") == project:
            by_item.setdefault(e["subject"], []).append(e["tool"])
    return [Finding(
        "high", "fusion-tools-bypassed", label,
        f"{', '.join(names)} bypassed by RenderFlow for editing - off in playback and in any render",
        "An earlier fix --apply switched these to pass-through so the clip plays in real time. "
        "Put them back before delivery:  python -m renderflow fix --restore-tools  (keeps every "
        "other fix) or fix --undo.") for label, names in by_item.items()]


def _changed_since(entry: dict[str, Any], current: Any) -> bool:
    """True if the value is no longer the one we set (the user changed it since).
    Entries from before ``new`` was journaled are restored unconditionally."""
    return "new" in entry and str(current) != str(entry["new"])


def project_name(project) -> str:
    try:
        return str(project.GetName() or "")
    except Exception:
        return ""


def sweep_markers(project) -> int:
    """Delete every marker tagged as ours from the current timeline. Returns how many."""
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        return 0
    try:
        markers = timeline.GetMarkers() or {}
    except Exception:
        return 0
    count = 0
    for m in markers.values():
        custom = str(m.get("customData") or "")
        if custom.startswith(MARKER_TAG + ":") and timeline.DeleteMarkerByCustomData(custom):
            count += 1
    return count
