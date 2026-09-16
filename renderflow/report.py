"""One run, one answer: what is slow, why, and what to do about it.

Runs the scan, the decode profiler and the render-cost profiler, merges their
findings into a single ranked list, and plans the fixes the measurements
justify. ``text()`` is for a person; ``to_dict()`` is for a tool or an agent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from renderflow.fix import Action, bypassed_findings, plan, plan_text
from renderflow.profile import DecodeMeasure, FFmpegMissing, measured_text, profile
from renderflow.rendercost import RenderProfile, apply_render_measurements, render_cost, render_findings
from renderflow.scan import Finding, ScanReport, findings_text, scan, sort_findings
from renderflow.tools import ToolReport, attribute, tool_findings


@dataclass
class FullReport:
    scan: ScanReport
    decode: dict[str, DecodeMeasure] = field(default_factory=dict)
    render: RenderProfile | None = None
    tools: ToolReport | None = None
    findings: list[Finding] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)        # about the plan, e.g. findings not markable
    skipped: list[str] = field(default_factory=list)      # stages not run, and why

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan": self.scan.to_dict(),
            "render": self.render.to_dict() if self.render else None,
            "tools": self.tools.to_dict() if self.tools else None,
            "findings": [asdict(f) for f in self.findings],
            "actions": [{**asdict(a), "estimate_s": round(a.estimate_s, 1)} for a in self.actions],
            "notes": self.notes,
            "skipped": self.skipped,
        }

    def text(self) -> str:
        parts = [self.scan.inventory_text()]
        if self.decode:
            parts.append("DECODE (FFmpeg, CPU)\n" + measured_text(self.scan, self.decode))
        if self.render:
            parts.append("RENDER (Resolve's queue)\n" + self.render.text())
        if self.tools:
            parts.append("FUSION TOOLS (each bypassed in turn)\n" + self.tools.text())
        if self.skipped:
            parts.append("skipped: " + "; ".join(self.skipped))
        parts.append("FINDINGS\n" + findings_text(
            self.findings, "none - nothing measured or observed looks like a bottleneck."))
        parts.append("PLAN\n" + plan_text(self.actions, self.notes) +
                     ("\n\napply with:  python -m renderflow fix --apply" if self.actions else ""))
        return "\n\n".join(parts)


def full_report(resolve, decode: bool = True, render: bool = True, proxies: str = "auto",
                tools: bool = False, progress: Callable[[str], None] | None = None,
                **render_kw) -> FullReport:
    say = progress or (lambda _m: None)
    say("scanning ...")
    scan_report = scan(resolve)
    report = FullReport(scan_report)

    if decode:
        try:
            report.decode = profile(scan_report, progress=say)
        except FFmpegMissing as exc:
            report.skipped.append(f"decode profiling ({exc})")
    else:
        report.skipped.append("decode profiling (disabled)")

    if render and scan_report.timeline is not None:
        try:
            report.render = render_cost(resolve, progress=say, **render_kw)
        except RuntimeError as exc:
            report.skipped.append(f"render-cost profiling ({exc})")
    elif render:
        report.skipped.append("render-cost profiling (no timeline open)")
    else:
        report.skipped.append("render-cost profiling (disabled)")

    if report.render:
        apply_render_measurements(scan_report, report.render)
    findings = list(scan_report.findings)
    if report.render:
        findings.extend(render_findings(report.render))
    if tools and report.render:
        try:
            report.tools = attribute(resolve, report.render, progress=say)
            findings.extend(tool_findings(report.tools))
        except RuntimeError as exc:
            report.skipped.append(f"Fusion tool attribution ({exc})")
    elif tools:
        report.skipped.append("Fusion tool attribution (needs the render measurement)")
    bypassed = bypassed_findings(scan_report.project)
    report.findings = sort_findings(findings + bypassed)
    report.actions = plan(scan_report, report.render, report.findings, proxies=proxies,
                          notes=report.notes, tools=report.tools)
    if bypassed:
        report.notes.append(f"{len(bypassed)} clip(s) have Fusion tools bypassed by an earlier apply - "
                            "re-enable before delivery:  python -m renderflow fix --restore-tools")
    return report
