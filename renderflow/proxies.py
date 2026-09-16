"""Make proxy media Resolve will accept, with FFmpeg.

A Resolve proxy must match the source frame for frame: same frame rate, same
length, same start timecode. Resolution may differ (same aspect). We write
DNxHR LB in QuickTime - the codec Resolve itself generates proxies in - which
is intra-frame, cheap to decode, and directly seekable, so both playback and
scrubbing stop depending on the source codec.

Resolution: sources wider than ``max_width`` are scaled down to it (4K -> HD);
HD and smaller stay full size, because for them the codec is the cost, not
the pixel count.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from renderflow.profile import require_ffmpeg
from renderflow.scan import ClipInfo

DEFAULT_PROXY_DIR = Path.home() / "Videos" / "RenderFlow Proxies"
DEFAULT_MAX_WIDTH = 1920


@dataclass
class ProxySpec:
    source: str
    target: str
    width: int
    height: int
    timecode: str | None
    seconds: float


def proxy_size(width: int, height: int, max_width: int = DEFAULT_MAX_WIDTH) -> tuple[int, int]:
    if width <= max_width or width <= 0:
        return width, height
    new_h = round(height * max_width / width)
    return max_width, new_h - (new_h % 2)


def proxy_path(clip: ClipInfo, out_dir: Path | str = DEFAULT_PROXY_DIR) -> Path:
    """Stable, unique output name: <stem>_<8 hex of source path>.mov."""
    stem = re.sub(r"[^\w.-]+", "_", Path(clip.path).stem)[:60]
    digest = hashlib.sha1(clip.path.encode("utf-8")).hexdigest()[:8]
    return Path(out_dir) / f"{stem}_{digest}.mov"


def plan_proxy(clip: ClipInfo, out_dir: Path | str = DEFAULT_PROXY_DIR,
               max_width: int = DEFAULT_MAX_WIDTH) -> ProxySpec:
    width, height = proxy_size(clip.width, clip.height, max_width)
    return ProxySpec(clip.path, str(proxy_path(clip, out_dir)), width, height,
                     clip.start_tc or None, clip.seconds)


def ffmpeg_command(exe: str, spec: ProxySpec, source_width: int) -> list[str]:
    cmd = [exe, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", spec.source,
           "-map", "0:v:0", "-map", "0:a?",
           "-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]
    if spec.width and spec.width != source_width:
        cmd += ["-vf", f"scale={spec.width}:{spec.height}"]
    cmd += ["-c:a", "pcm_s16le"]
    if spec.timecode:
        cmd += ["-timecode", spec.timecode]
    cmd += [spec.target]
    return cmd


Runner = Callable[[list], "subprocess.CompletedProcess[str]"]      # bare list: this line runs on 3.8


def _run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def generate_proxy(spec: ProxySpec, name: str, source_width: int, exe: str | None = None,
                   runner: Runner = _run, progress: Callable[[str], None] | None = None) -> float:
    """Write the proxy file. Returns wall seconds taken. Raises RuntimeError on failure."""
    exe = require_ffmpeg(exe)
    Path(spec.target).parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"encoding proxy for {name} ({spec.width}x{spec.height} DNxHR LB, "
                 f"{spec.seconds:.0f}s of video) ...")
    started = time.perf_counter()
    result = runner(ffmpeg_command(exe, spec, source_width))
    took = time.perf_counter() - started
    if result.returncode != 0 or not os.path.isfile(spec.target):
        try:
            os.remove(spec.target)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg failed for {name}: {(result.stderr or '').strip()[:400]}")
    if progress:
        rate = spec.seconds / took if took else 0.0
        progress(f"  done in {took:.0f}s ({rate:.1f}x real time)")
    return took
