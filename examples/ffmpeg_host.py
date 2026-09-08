"""A real, working host: an FFmpeg-backed preview cache.

This is a complete integration you can actually use and adapt. It renders
timeline segments through FFmpeg with a configurable filter chain and caches the
result to disk, while FrameForge decides the order based on where the playhead
is and which segments are expensive.

    # see what it would do, no FFmpeg needed
    python examples/ffmpeg_host.py --dry-run

    # cache a real file, prioritising around frame 900
    python examples/ffmpeg_host.py --input clip.mp4 --playhead 900

    # follow a moving playhead (simulates an editor scrubbing)
    python examples/ffmpeg_host.py --input clip.mp4 --follow

Adapting this to your own renderer means replacing ``FFmpegHost.render``.
Nothing else changes.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CacheEngine, EngineConfig, Segment, describe_host
from frameforge.formats.native import from_dict
from frameforge.simulation import FakeEditor

#: Deliberately expensive filter chains, so cost differences are visible.
FILTER_PRESETS = {
    "none": "null",
    "blur": "gblur=sigma=8",
    "denoise": "hqdn3d=8:6:12:9",
    "heavy": "hqdn3d=10:8:14:10,gblur=sigma=12,unsharp=7:7:2.5",
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class FFmpegHost:
    """Renders segments to a disk cache with FFmpeg.

    The only method FrameForge requires is ``render``. ``is_cached``, ``evict``
    and ``cost_hint`` are optional; they are here to show what implementing them
    buys you.
    """

    def __init__(self, source: Path, cache_dir: Path, fps: float, dry_run: bool = False):
        self.source = source
        self.cache_dir = cache_dir
        self.fps = fps
        self.dry_run = dry_run
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.rendered: list[str] = []

    def _path(self, segment: Segment) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in segment.name)
        return self.cache_dir / f"{safe}.mkv"

    # --- required -----------------------------------------------------------
    def render(self, segment: Segment) -> None:
        out = self._path(segment)
        self.rendered.append(segment.name)
        if self.dry_run:
            time.sleep(0.004 * segment.cost)  # pretend
            out.write_bytes(b"")
            return

        chain = ",".join(
            FILTER_PRESETS[p] for p in self._filters_for(segment)
        ) or "null"
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-ss", str(segment.start / self.fps),
            "-i", str(self.source),
            "-frames:v", str(segment.length),
            "-vf", chain,
            "-c:v", "ffv1",          # fast lossless: cache write shouldn't dominate
            "-an",
            str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    @staticmethod
    def _filters_for(segment: Segment) -> list[str]:
        """Map the segment's effects onto FFmpeg filters."""
        chosen = []
        for effect in segment.effects:
            if effect in ("Noise Reduction", "Temporal Noise Reduction"):
                chosen.append("denoise")
            elif effect in ("Blur", "Motion Blur"):
                chosen.append("blur")
            elif effect == "Composite":
                chosen.append("heavy")
        return chosen or ["none"]

    # --- optional -----------------------------------------------------------
    def is_cached(self, segment: Segment) -> bool:
        return self._path(segment).exists()

    def evict(self, segment: Segment) -> None:
        self._path(segment).unlink(missing_ok=True)


TIMELINE = {
    "name": "ffmpeg-demo",
    "fps": 24,
    "clips": [
        {"name": "open", "start": 0, "end": 240, "effects": []},
        {"name": "blurred", "start": 240, "end": 480, "effects": ["Blur"]},
        {"name": "clean", "start": 480, "end": 720, "effects": []},
        {"name": "denoised", "start": 720, "end": 1080, "effects": ["Noise Reduction"]},
        {"name": "stacked", "start": 1080, "end": 1320, "effects": ["Composite"]},
        {"name": "close", "start": 1320, "end": 1560, "effects": ["Blur"]},
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="FrameForge FFmpeg preview cache")
    ap.add_argument("--input", type=Path, help="source video file")
    ap.add_argument("--cache-dir", type=Path, help="where cached segments go")
    ap.add_argument("--playhead", type=float, default=900.0)
    ap.add_argument("--follow", action="store_true",
                    help="simulate a scrubbing editor instead of a fixed playhead")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't invoke FFmpeg; just show the scheduling order")
    ap.add_argument("--budget", type=int, default=0,
                    help="cache budget in frames (0 = unlimited)")
    args = ap.parse_args()

    dry = args.dry_run or not args.input
    if not dry and not ffmpeg_available():
        print("ffmpeg not found on PATH; falling back to --dry-run\n")
        dry = True
    if not dry and not args.input.exists():
        print(f"{args.input} does not exist; falling back to --dry-run\n")
        dry = True

    timeline = from_dict(TIMELINE)
    cache_dir = args.cache_dir or Path(tempfile.mkdtemp(prefix="frameforge_cache_"))
    host = FFmpegHost(args.input or Path("<none>"), cache_dir, timeline.fps, dry_run=dry)

    print(f"FrameForge FFmpeg cache  ({'DRY RUN' if dry else args.input})")
    print(f"cache dir: {cache_dir}\n")
    print(describe_host(host), "\n")

    engine = CacheEngine(
        host,
        timeline,
        config=EngineConfig(cache_budget_frames=args.budget or None),
    )
    print(f"{len(timeline)} clips -> {engine.pending} segments\n")

    if args.follow:
        editor = FakeEditor(timeline, seed=3)
        for position in list(editor.mixed_session())[::12]:
            engine.set_playhead(position)
            for result in engine.step(1):
                print(f"  playhead={position:7.0f} -> {result.segment.name:<10} "
                      f"cost~{result.segment.cost:5.1f}  {result.seconds * 1000:7.1f} ms")
            if not engine.pending:
                break
    else:
        engine.set_playhead(args.playhead)
        print(f"caching in priority order from frame {args.playhead:.0f}:")
        while engine.pending:
            results = engine.step(1)
            if not results:
                break
            r = results[0]
            status = "ok" if r.ok else f"FAILED {r.error}"
            print(f"  {r.segment.name:<10} cost~{r.segment.cost:5.1f}  "
                  f"{r.seconds * 1000:8.1f} ms  {status}")

    print("\nstats:", engine.stats())
    if dry:
        shutil.rmtree(cache_dir, ignore_errors=True)
    else:
        print(f"\ncached segments are in {cache_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
