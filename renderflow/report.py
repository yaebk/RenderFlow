"""One run, one answer: what is slow, why, and what to do about it.

Runs the scan, the decode profiler and the render-cost profiler, merges their
findings into a single ranked list, and plans the fixes the measurements
justify. ``text()`` is for a person; ``to_dict()`` is for a tool or an agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from renderflow.fix import Action, plan, plan_text
from renderflow.profile import DecodeMeasure, FFmpegMissing, measured_text, profile
from renderflow.rendercost import RenderProfile, render_cost, render_findings
from renderflow.scan import Finding, ScanReport, scan

SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


@dataclass
class FullReport:
    scan: ScanReport
    decode: dict[str, DecodeMeasure] = field(default_factory=dict)
    render: RenderProfile | None = None
    findings: list[Finding] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)      # stages not run, and why

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan": self.scan.to_dict(),
            "render": self.render.to_dict() if self.render else None,
            "findings": [f.__dict__ for f in self.findings],
            "actions": [{"kind": a.kind, "subject": a.subject, "summary": a.summary,
                         "why": a.why, "estimate_s": round(a.estimate_s, 1), "params": a.params}
                        for a in self.actions],
            "skipped": self.skipped,
        }

    def text(self) -> str:
        parts = [self.scan.text().split("\n--- ")[0].rstrip()]        # header + clip table only
        if self.decode:
            parts.append("DECODE (FFmpeg, CPU)\n" + measured_text(self.scan, self.decode))
        if self.render:
            parts.append("RENDER (Resolve's queue)\n" + self.render.text())
        if self.skipped:
            parts.append("skipped: " + "; ".join(self.skipped))
        parts.append("FINDINGS\n" + findings_text(self.findings))
        parts.append("PLAN\n" + plan_text(self.actions) +
                     ("\n\napply with:  python -m renderflow fix --apply" if self.actions else ""))
        return "\n\n".join(parts)


def findings_text(findings: list[Finding]) -> str:
    if not findings:
        return "none - nothing measured or observed looks like a bottleneck."
    lines = []
    for severity in ("high", "medium", "info"):
        group = [f for f in findings if f.severity == severity]
        if group:
            lines.append(f"--- {severity} ({len(group)}) ---")
            lines.extend(str(f) for f in group)
    return "\n".join(lines)


def full_report(resolve, decode: bool = True, render: bool = True, proxies: str = "auto",
                progress: Callable[[str], None] | None = None, **render_kw) -> FullReport:
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

    findings = list(scan_report.findings)
    if report.render:
        findings.extend(render_findings(report.render))
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))
    report.findings = findings
    report.actions = plan(scan_report, report.render, findings, proxies=proxies)
    return report
