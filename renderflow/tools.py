"""Which Fusion tool is the expensive one - measured, not guessed.

The render-cost profiler says a clip renders at 270 ms per frame and carries
sixteen Fusion tools. This takes that clip and bypasses each tool in turn
(Fusion's own pass-through switch, ``TOOLB_PassThrough`` - the same thing as
clicking a node's bypass button), renders the same sample again, and reports
how much each tool's absence saves:

    Adjustment Clip @V4 01:00:01:28    270 ms/frame
      Grain1              Grain          saves ~105 ms/frame
      changecolor_3_1_2   FastNoise      saves ~105 ms/frame
      ColorCorrector1     ColorCorrector saves  ~63 ms/frame

Two things about the numbers. Resolve reports job time in roughly half-second
steps, so on a short clip each figure is +/- 10-20 ms/frame; the ranking is
reliable, the decimals are not. And the savings overlap - bypassing an
upstream tool spares downstream tools work too - so they do not add up to
the clip's total.

NOTHING IS DELETED OR EDITED. Only the pass-through flag is touched, only on
tools that were active, and only for the duration of one sample render: the
original state of every tool is recorded before anything changes, each tool
is put back right after its sample, every tool is checked afterwards, and the
tools currently bypassed are listed in ~/.renderflow/bypassed.json so that a
run killed halfway can be repaired by the next run (or by ``fix --undo``).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from renderflow.rendercost import RenderProfile, RenderQueue, RenderSample
from renderflow.scan import FUSION_PASSTHROUGH, Finding, _int, item_label, sort_findings

BYPASSED_PATH = Path.home() / ".renderflow" / "bypassed.json"
PASS_THROUGH = "TOOLB_PassThrough"
# Not operators: routing, animation curves, audio, notes. Bypassing them measures nothing.
NOT_OPERATORS = FUSION_PASSTHROUGH | {"LUTBezier", "BezierSpline", "PolyPath", "XYPath", "Path",
                                      "PipeRouter", "Note", "Underlay"}
DEFAULT_FRAMES = 90             # sample length; longer = finer than Resolve's 0.5 s job clock
MIN_FRAMES = 12
DEFAULT_MAX_TOOLS = 16
DEFAULT_MAX_COMPS = 6
STEP_MS = 500.0                 # Resolve's job clock; the +/- on every saving is STEP_MS / frames


@dataclass
class ToolCost:
    name: str                   # TOOLS_Name, unique within the comp
    kind: str                   # TOOLS_RegID
    saved_ms_per_frame: float   # what bypassing this one tool alone saves
    status: str = "Complete"    # of the render with it bypassed; a comp may not render without a tool

    @property
    def ok(self) -> bool:
        return self.status == "Complete"


@dataclass
class CompCost:
    label: str                  # the timeline item measured
    track: int
    start: int
    end: int
    comp: int                   # 1-based Fusion comp index on the item
    frames: int                 # sample length rendered
    ms_per_frame: float         # with everything on, job set-up removed
    tools: list[ToolCost] = field(default_factory=list)
    copies: list[str] = field(default_factory=list)     # other items with the identical comp
    status: str = "Complete"

    @property
    def ok(self) -> bool:
        return self.status == "Complete"

    @property
    def measured(self) -> list[ToolCost]:
        return sorted((t for t in self.tools if t.ok), key=lambda t: -t.saved_ms_per_frame)

    @property
    def step_ms(self) -> float:
        return STEP_MS / self.frames if self.frames else 0.0


@dataclass
class ToolReport:
    timeline: str
    fps: float
    comps: list[CompCost]
    problems: list[str] = field(default_factory=list)   # anything not put back - must be empty

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def text(self) -> str:
        if not self.comps:
            return "no Fusion comps to attribute - nothing measured heavy carries Fusion tools."
        lines = []
        for c in self.comps:
            copies = f"  (+{len(c.copies)} more copies of this comp)" if c.copies else ""
            lines.append(f"{c.label}{copies}")
            if not c.ok:
                lines.append(f"  {c.status}")
                lines.append("")
                continue
            lines.append(f"  {c.ms_per_frame:.0f} ms/frame with every tool on, over {c.frames} frames; "
                         "bypassing one tool alone saves:")
            width = max((len(t.name) for t in c.tools), default=0)
            for t in c.measured:
                lines.append(f"  {t.name:<{width}}  {t.kind:<16} {t.saved_ms_per_frame:>6.0f} ms/frame")
            for t in c.tools:
                if not t.ok:
                    lines.append(f"  {t.name:<{width}}  {t.kind:<16}      - the comp does not render "
                                 f"with it bypassed ({t.status})")
            if not c.tools:
                lines.append("  no tool could be bypassed")
            lines.append("")
        step = max((c.step_ms for c in self.comps if c.ok), default=0.0)
        lines.append(f"+/- {step:.0f} ms/frame: Resolve reports job time in {STEP_MS / 1000:g} s steps. "
                     "Savings overlap, so they do not add up to the total.")
        if self.problems:
            lines.append("")
            lines.append("PROBLEMS - check these tools in Fusion: " + "; ".join(self.problems))
        return "\n".join(lines)


# ------------------------------------------------------------- the guard
class Bypassed:
    """Every tool this process has switched to pass-through, kept on disk until restored.

    Written before a tool is bypassed, cleared after it is put back, so a run
    that dies in between leaves a record :func:`restore_bypassed` can act on.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else BYPASSED_PATH       # looked up at call time (tests move it)
        self.entries: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text("utf-8")).get("entries", [])
            except (OSError, ValueError):
                self.entries = []

    def add(self, entry: dict[str, Any]) -> None:
        self.entries.append({**entry, "at": time.time()})
        self._save()

    def remove(self, entry: dict[str, Any]) -> None:
        self.entries = [e for e in self.entries if not _same(e, entry)]
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        if self.entries:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"entries": self.entries}, indent=1), "utf-8")
        elif self.path.exists():
            self.path.unlink()


def _same(a: dict, b: dict) -> bool:
    keys = ("timeline", "track", "start", "comp", "tool")
    return all(a.get(k) == b.get(k) for k in keys)


def _passing(tool) -> bool:
    try:
        return bool((tool.GetAttrs() or {}).get(PASS_THROUGH))
    except Exception:
        return False


def _set_pass(tool, value: bool) -> bool:
    """Set the flag and confirm by reading it back."""
    try:
        tool.SetAttrs({PASS_THROUGH: bool(value)})
    except Exception:
        return False
    return _passing(tool) == bool(value)


def restore_bypassed(project, record: Bypassed | None = None) -> list[str]:
    """Re-enable tools an interrupted run left bypassed. Returns what it fixed."""
    record = record if record is not None else Bypassed()
    fixed: list[str] = []
    for entry in list(record.entries):
        tool = _find_tool(project, entry)
        if tool is None:
            continue                                # timeline or item gone; keep the record
        if not _passing(tool) or _set_pass(tool, False):
            fixed.append(f"{entry['tool']} on {entry['label']}")
            record.remove(entry)
    return fixed


def _find_tool(project, entry: dict):
    timeline = project.GetCurrentTimeline()
    if timeline is None or str(timeline.GetName()) != entry.get("timeline"):
        return None
    for item in timeline.GetItemListInTrack("video", entry["track"]) or []:
        if _int(item.GetStart()) != entry["start"]:
            continue
        try:
            comp = item.GetFusionCompByIndex(entry["comp"])
            for tool in (comp.GetToolList() or {}).values():
                if str((tool.GetAttrs() or {}).get("TOOLS_Name")) == entry["tool"]:
                    return tool
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- measure
def _real_tools(comp) -> list[tuple[str, str, Any]]:
    """(name, kind, tool) for every operator in the comp, in tool-list order."""
    out = []
    for tool in (comp.GetToolList() or {}).values():
        attrs = tool.GetAttrs() or {}
        kind = str(attrs.get("TOOLS_RegID") or "")
        if kind and kind not in NOT_OPERATORS:
            out.append((str(attrs.get("TOOLS_Name") or kind), kind, tool))
    return out


def _signature(tools: list[tuple[str, str, Any]]) -> tuple:
    return tuple((name, kind) for name, kind, _ in tools)


def candidates(profile: RenderProfile) -> list[RenderSample]:
    """Items worth attributing: rendered below real time and carrying Fusion tools,
    on their own or inside a heavy stretch."""
    heavy_items = [s for s in profile.samples if s.ok and not s.stretch
                   and profile.ratio(s) < 1.0 and s.fusion_tools]
    in_stretch: set[str] = set()
    for s in profile.samples:
        if s.ok and s.stretch and profile.ratio(s) < 1.0:
            in_stretch.update(s.clips)
    short = [s for s in profile.samples if s.too_short and s.fusion_tools and s.label in in_stretch]
    return heavy_items + short


def attribute(resolve, profile: RenderProfile, frames: int = DEFAULT_FRAMES,
              max_tools: int = DEFAULT_MAX_TOOLS, max_comps: int = DEFAULT_MAX_COMPS,
              progress: Callable[[str], None] | None = None, queue: RenderQueue | None = None,
              record: Bypassed | None = None) -> ToolReport:
    """Measure what each Fusion tool costs on every clip the profile found heavy.

    Comps that are tool-for-tool identical (a macro dropped on seven clips) are
    measured once, on the first; the rest are listed as copies.
    """
    say = progress or (lambda _m: None)
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise RuntimeError("no timeline is open in Resolve")
    record = record if record is not None else Bypassed()
    fixed = restore_bypassed(project, record)
    if fixed:
        say(f"re-enabled {len(fixed)} tool(s) an interrupted run left bypassed: {', '.join(fixed)}")
    fps = profile.fps
    overhead = _overhead(profile)
    report = ToolReport(str(timeline.GetName()), fps, [])
    wanted = {(s.track, s.start): s for s in candidates(profile)}
    if not wanted:
        return report

    # Walk the timeline once for the item objects; group identical comps.
    seen: dict[tuple, CompCost] = {}
    jobs: list[tuple[CompCost, list, Any]] = []
    for track in range(1, _int(timeline.GetTrackCount("video")) + 1):
        for item in timeline.GetItemListInTrack("video", track) or []:
            key = (track, _int(item.GetStart()))
            if key not in wanted:
                continue
            label = item_label(str(item.GetName()), track, key[1], fps)
            for index in range(1, _int(item.GetFusionCompCount()) + 1):
                comp = item.GetFusionCompByIndex(index)
                tools = _real_tools(comp) if comp is not None else []
                if not tools:
                    continue
                sig = _signature(tools)
                if sig in seen:
                    seen[sig].copies.append(label)
                    continue
                cost = CompCost(label, track, key[1], _int(item.GetEnd()), index, 0, 0.0)
                seen[sig] = cost
                length = cost.end - cost.start
                if length < MIN_FRAMES:
                    cost.status = f"too short to measure ({length} frames)"
                    report.comps.append(cost)
                    continue
                jobs.append((cost, tools[:max_tools], item))
    # Comps with the most tools first: with a cap, those are the ones worth the renders.
    jobs.sort(key=lambda job: -len(job[1]))
    jobs = jobs[:max_comps]
    if not jobs:
        return report

    queue = queue or RenderQueue(resolve)
    with queue:
        for number, (cost, tools, item) in enumerate(jobs, 1):
            length = cost.end - cost.start
            n = min(length, frames)
            a = cost.start + (length - n) // 2
            b = a + n - 1
            cost.frames = n
            say(f"comp {number}/{len(jobs)}: {cost.label} - {len(tools)} tool(s), {n} frames each")
            base, status = queue.render_range(a, b)
            if status != "Complete":
                cost.status = f"sample render did not complete ({status})"
                report.comps.append(cost)
                continue
            cost.ms_per_frame = max(0.0, base - overhead) / n
            for name, kind, tool in tools:
                if _passing(tool):
                    continue                        # the editor's own bypass: leave it, measure nothing
                entry = {"timeline": report.timeline, "label": cost.label, "track": cost.track,
                         "start": cost.start, "comp": cost.comp, "tool": name}
                record.add(entry)                   # on disk before the flag flips
                if not _set_pass(tool, True):
                    record.remove(entry)
                    continue
                try:
                    ms, status = queue.render_range(a, b)
                finally:
                    if _set_pass(tool, False):
                        record.remove(entry)
                    else:
                        report.problems.append(f"{name} ({kind}) on {cost.label} may still be bypassed")
                saved = max(0.0, base - ms) / n if status == "Complete" else 0.0
                cost.tools.append(ToolCost(name, kind, saved, status))
            report.comps.append(cost)
    return report


def _overhead(profile: RenderProfile) -> float:
    two = [s.overhead_ms for s in profile.samples if s.two_point]
    return sum(two) / len(two) if two else 0.0


# --------------------------------------------------------------- findings
def tool_findings(report: ToolReport, fps: float | None = None) -> list[Finding]:
    """One finding per measured comp naming the tools that cost the most."""
    fps = fps or report.fps
    frame_ms = 1000.0 / fps if fps else 0.0
    out: list[Finding] = []
    for c in report.comps:
        if not c.ok or c.ms_per_frame < frame_ms:          # renders in real time: nothing to blame
            continue
        # A saving inside the job clock's noise (two steps) or under half a frame is not a result.
        big = [t for t in c.measured if t.saved_ms_per_frame >= max(2 * c.step_ms, frame_ms * 0.5)]
        if not big:
            continue
        top = ", ".join(f"{t.name} ({t.kind}, ~{t.saved_ms_per_frame:.0f} ms/frame)" for t in big[:3])
        copies = f" The same comp is on {len(c.copies)} more clip(s)." if c.copies else ""
        out.append(Finding(
            "high" if c.ms_per_frame > frame_ms * 2 else "medium", "fusion-tool-heavy", c.label,
            f"{c.ms_per_frame:.0f} ms/frame; the cost is mostly {top}",
            "Measured by bypassing each tool in turn and rendering the same frames again. "
            "Bypass the heavy ones while editing (the node's pass-through switch) and re-enable "
            "them for delivery, or bake this clip once it is final. Noise generators (Grain, "
            "FastNoise) and blurs cost the same every frame and cache well." + copies))
    return sort_findings(out)
