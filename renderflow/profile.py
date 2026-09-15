"""Measure, on this machine, how fast each source clip actually decodes.

The scan guesses from the codec name. This times it: FFmpeg decodes a sample
from the middle of the file with the CPU, and we compare frames-per-second
achieved against the clip's own frame rate. A second test times random-access
seeks, which is what scrubbing feels like.

    realtime_ratio = decoded fps / clip fps
        < 1.0   cannot play in real time on this machine
        < 2.0   plays, but with no headroom for grading or effects
        >= 2.0  decode is not the bottleneck

WHY FFMPEG STANDS IN FOR RESOLVE
--------------------------------
Resolve does not expose its decoder for timing. FFmpeg's software decoders
are at least as fast as the ones the free edition uses, so a clip FFmpeg
cannot decode in real time is one Resolve cannot either, and the ratio is an
upper bound on what playback will manage before any grading or effects.

Results are cached per file (path, size, mtime, ffmpeg build, cpu) in
~/.renderflow/measurements.json so a project is measured once.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from renderflow.scan import ClipInfo, Finding, ScanReport, sort_findings

DEFAULT_SAMPLE_S = 5.0
DEFAULT_SEEKS = 5
CACHE_PATH = Path.home() / ".renderflow" / "measurements.json"
CACHE_VERSION = 1


class FFmpegMissing(RuntimeError):
    pass


@dataclass
class DecodeMeasure:
    path: str
    sample_seconds: float
    frames: int
    wall_seconds: float
    decode_fps: float
    clip_fps: float
    realtime_ratio: float
    seek_ms: float | None          # mean time to land on a random frame, process overhead removed
    hwaccel: str | None
    ffmpeg: str
    cpu: str
    measured_at: float

    @property
    def verdict(self) -> str:
        if self.realtime_ratio < 1.0:
            return "below-realtime"
        if self.realtime_ratio < 2.0:
            return "marginal"
        return "ok"


# ----------------------------------------------------------------- ffmpeg
def find_ffmpeg() -> str | None:
    """ffmpeg on PATH, or where winget puts it before the shell is restarted."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe")]
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                             "*FFmpeg*", "*", "bin", "ffmpeg.exe"))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
    return None


def require_ffmpeg(exe: str | None = None) -> str:
    exe = exe or find_ffmpeg()
    if not exe:
        raise FFmpegMissing(
            "ffmpeg not found - install it (Windows: winget install Gyan.FFmpeg) and open a new terminal"
        )
    return exe


Runner = Callable[[list], "tuple[str, str, float]"]


def run_command(cmd: list) -> tuple[str, str, float]:
    """Run and return (stdout, stderr, wall seconds)."""
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return proc.stdout, proc.stderr, time.perf_counter() - started


def ffmpeg_version(exe: str, runner: Runner = run_command) -> str:
    out, _, _ = runner([exe, "-version"])
    first = out.splitlines()[0] if out else ""
    return first.replace("ffmpeg version ", "").split(" ")[0] or "unknown"


def parse_progress(stdout: str) -> int:
    """Frame count from ``-progress pipe:1`` output (last ``frame=`` wins)."""
    frames = 0
    for line in stdout.splitlines():
        if line.startswith("frame="):
            try:
                frames = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    return frames


# ---------------------------------------------------------------- measure
def measure_decode(path: str, clip_fps: float, duration_s: float | None = None,
                   sample_s: float = DEFAULT_SAMPLE_S, seeks: int = DEFAULT_SEEKS,
                   hwaccel: str | None = None, exe: str | None = None,
                   runner: Runner = run_command, version: str | None = None) -> DecodeMeasure:
    """Decode a ``sample_s`` slice from the middle of ``path`` and time it.

    ``version`` is the ffmpeg build string, looked up if not given.
    """
    exe = require_ffmpeg(exe)
    duration_s = duration_s or 0.0
    start = max(0.0, duration_s / 2 - sample_s / 2) if duration_s > sample_s else 0.0

    cmd = [exe, "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-ss", f"{start:.3f}", "-i", path, "-t", f"{sample_s:.3f}", "-an",
            "-f", "null", "-"]
    out, err, wall = runner(cmd)
    frames = parse_progress(out)
    if frames <= 0:
        raise RuntimeError(f"ffmpeg decoded no frames from {path}: {err.strip()[:300]}")
    decode_fps = frames / wall if wall > 0 else 0.0

    seek_ms = _measure_seeks(exe, path, duration_s, seeks, hwaccel, runner) if seeks else None

    return DecodeMeasure(
        path=path,
        sample_seconds=sample_s,
        frames=frames,
        wall_seconds=round(wall, 3),
        decode_fps=round(decode_fps, 1),
        clip_fps=clip_fps,
        realtime_ratio=round(decode_fps / clip_fps, 2) if clip_fps else 0.0,
        seek_ms=seek_ms,
        hwaccel=hwaccel,
        ffmpeg=version or ffmpeg_version(exe, runner),
        cpu=platform.processor() or platform.machine(),
        measured_at=time.time(),
    )


def _measure_seeks(exe, path, duration_s, seeks, hwaccel, runner) -> float:
    """Mean wall time to decode one frame at spread-out positions.

    Process start-up and teardown are measured on a tiny synthetic input and
    subtracted, so the number is about the file, not the OS.
    """
    _, _, overhead = runner([exe, "-hide_banner", "-nostdin", "-v", "error",
                             "-f", "lavfi", "-i", "nullsrc=s=16x16:d=0.1",
                             "-frames:v", "1", "-f", "null", "-"])
    span = duration_s if duration_s > 0 else 10.0
    total = 0.0
    for k in range(seeks):
        position = span * (k + 0.5) / seeks
        cmd = [exe, "-hide_banner", "-nostdin", "-v", "error"]
        if hwaccel:
            cmd += ["-hwaccel", hwaccel]
        cmd += ["-ss", f"{position:.3f}", "-i", path, "-frames:v", "1", "-an", "-f", "null", "-"]
        _, _, wall = runner(cmd)
        total += max(0.0, wall - overhead)
    return round(1000.0 * total / seeks, 1)


# ------------------------------------------------------------------ cache
def _cache_key(path: str, ffmpeg: str, hwaccel: str | None, sample_s: float) -> str | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    raw = f"{path}|{stat.st_size}|{int(stat.st_mtime)}|{ffmpeg}|{hwaccel}|{sample_s}|{platform.processor()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class MeasurementCache:
    def __init__(self, path: Path | str | None = CACHE_PATH):
        self.path = Path(path) if path else None
        self.data: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if loaded.get("version") == CACHE_VERSION:
                    self.data = loaded.get("entries", {})
            except (OSError, ValueError):
                self.data = {}

    def get(self, key: str | None) -> DecodeMeasure | None:
        if key and key in self.data:
            try:
                return DecodeMeasure(**self.data[key])
            except TypeError:
                return None
        return None

    def put(self, key: str | None, measure: DecodeMeasure) -> None:
        if not key:
            return
        self.data[key] = asdict(measure)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"version": CACHE_VERSION, "entries": self.data}, indent=1),
                                 "utf-8")


# --------------------------------------------------------------- profile
def profile(report: ScanReport, sample_s: float = DEFAULT_SAMPLE_S, seeks: int = DEFAULT_SEEKS,
            hwaccel: str | None = None, exe: str | None = None, cache: MeasurementCache | None = None,
            runner: Runner = run_command, progress: Callable[[str], None] | None = None,
            ) -> dict[str, DecodeMeasure]:
    """Measure every reachable clip in ``report`` and replace codec guesses with numbers.

    Returns measurements by clip path. The report's clips gain ``measured``
    entries and its findings are rewritten: rule-of-thumb ``codec-long-gop``
    findings go, measured ``decode-*`` and ``seek-slow`` findings come in.
    """
    exe = require_ffmpeg(exe)
    version = ffmpeg_version(exe, runner)
    cache = cache if cache is not None else MeasurementCache()
    results: dict[str, DecodeMeasure] = {}

    for clip in report.clips:
        if clip.location == "missing" or not clip.path or clip.path in results:
            continue
        key = _cache_key(clip.path, version, hwaccel, sample_s)
        measure = cache.get(key)
        if measure is None:
            if progress:
                progress(f"measuring {clip.name} ...")
            try:
                measure = measure_decode(clip.path, clip.fps, clip.seconds, sample_s, seeks,
                                         hwaccel, exe, runner, version)
            except RuntimeError as exc:
                if progress:
                    progress(f"  skipped: {exc}")
                continue
            cache.put(key, measure)
        elif progress:
            progress(f"cached    {clip.name}")
        results[clip.path] = measure

    apply_measurements(report, results)
    return results


def apply_measurements(report: ScanReport, results: dict[str, DecodeMeasure]) -> None:
    measured_paths = set(results)
    report.findings = [
        f for f in report.findings
        if not (f.code == "codec-long-gop" and _clip_path(report, f.subject) in measured_paths)
    ]
    for clip in report.clips:
        measure = results.get(clip.path)
        if measure is None:
            continue
        clip.measured = asdict(measure)
        report.findings.extend(decode_findings(clip, measure))
    report.findings = sort_findings(report.findings)


def _clip_path(report: ScanReport, name: str) -> str | None:
    for clip in report.clips:
        if clip.name == name:
            return clip.path
    return None


def decode_findings(clip: ClipInfo, m: DecodeMeasure) -> list[Finding]:
    out: list[Finding] = []
    rate = f"{m.decode_fps:g} fps decoded vs {m.clip_fps:g} fps footage ({m.realtime_ratio:g}x real time)"
    if m.verdict == "below-realtime":
        out.append(Finding("high", "decode-below-realtime", clip.name,
                           f"cannot decode in real time: {rate}",
                           f"Measured with FFmpeg's CPU decoder on this machine ({m.cpu}). Resolve's "
                           "free-edition decoder is no faster, so this clip will drop frames before "
                           "any grading or effects are applied. A proxy fixes it."))
    elif m.verdict == "marginal":
        out.append(Finding("medium", "decode-marginal", clip.name,
                           f"decodes with little headroom: {rate}",
                           "Plays cleanly on its own, but every effect, grade or second layer eats "
                           "into the margin. A proxy is worthwhile if this clip stutters."))
    else:
        out.append(Finding("info", "decode-ok", clip.name,
                           f"decode is not the bottleneck: {rate}",
                           "If this clip stutters, look at effects, grades or the drive, not the codec."))
    if m.seek_ms is not None and m.seek_ms > 250:
        out.append(Finding("medium", "seek-slow", clip.name,
                           f"random access takes {m.seek_ms:g} ms per frame",
                           "Long-GOP files must decode from the previous keyframe to reach a frame, "
                           "which is what makes scrubbing feel sticky. Proxies (intra-frame) make "
                           "every frame directly reachable."))
    return out


def measured_text(report: ScanReport, results: dict[str, DecodeMeasure]) -> str:
    """Table of measurements to print above the findings."""
    if not results:
        return "no clips measured."
    lines = [f"{'clip':<34} {'codec':<18} {'fps':>5} {'decode':>8} {'ratio':>6} {'seek':>8}  verdict"]
    for clip in report.clips:
        m = results.get(clip.path)
        if m is None:
            continue
        name = (clip.name[:31] + "...") if len(clip.name) > 34 else clip.name
        seek = f"{m.seek_ms:g} ms" if m.seek_ms is not None else "-"
        lines.append(f"{name:<34} {clip.codec[:18]:<18} {m.clip_fps:>5g} {m.decode_fps:>8g} "
                     f"{m.realtime_ratio:>5g}x {seek:>8}  {m.verdict}")
    sample = next(iter(results.values()))
    lines.append("")
    lines.append(f"measured with ffmpeg {sample.ffmpeg} (CPU decode"
                 f"{', hwaccel ' + sample.hwaccel if sample.hwaccel else ''}) on {sample.cpu}")
    return "\n".join(lines)
