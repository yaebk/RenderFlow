"""Playback / cache metrics collection (Phase 5).

Feed the collector one sample per displayed frame during a playback run; it
produces the numbers the handoff asks to compare (dropped frames, average and
1%-low FPS, time-to-smooth, cache hit rate, wasted renders).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean


@dataclass
class Metrics:
    target_fps: float = 24.0

    frame_times_ms: list[float] = field(default_factory=list)
    dropped: int = 0
    displayed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    frames_rendered: int = 0
    frames_rendered_unused: int = 0
    rerenders_after_scrub: int = 0
    _first_smooth_frame: int | None = None
    _smooth_run: int = 0

    def record_frame(self, frame_time_ms: float, cache_hit: bool) -> None:
        self.displayed += 1
        self.frame_times_ms.append(frame_time_ms)
        budget = 1000.0 / self.target_fps
        if cache_hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        if frame_time_ms > budget * 1.5:
            self.dropped += 1
            self._smooth_run = 0
        else:
            self._smooth_run += 1
            if self._smooth_run >= self.target_fps and self._first_smooth_frame is None:
                self._first_smooth_frame = self.displayed - self._smooth_run

    def record_render(self, used: bool) -> None:
        self.frames_rendered += 1
        if not used:
            self.frames_rendered_unused += 1

    # ------------------------------------------------------------------ views
    @property
    def avg_fps(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        return 1000.0 / mean(self.frame_times_ms)

    @property
    def low_1pct_fps(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        ordered = sorted(self.frame_times_ms, reverse=True)
        n = max(1, len(ordered) // 100)
        return 1000.0 / mean(ordered[:n])

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total else 0.0

    @property
    def wasted_render_rate(self) -> float:
        return self.frames_rendered_unused / self.frames_rendered if self.frames_rendered else 0.0

    @property
    def time_to_smooth_s(self) -> float | None:
        if self._first_smooth_frame is None:
            return None
        return self._first_smooth_frame / self.target_fps

    def summary(self) -> dict[str, float | int | None]:
        return {
            "displayed_frames": self.displayed,
            "dropped_frames": self.dropped,
            "drop_rate": round(self.dropped / self.displayed, 4) if self.displayed else 0.0,
            "avg_fps": round(self.avg_fps, 2),
            "low_1pct_fps": round(self.low_1pct_fps, 2),
            "cache_hit_rate": round(self.hit_rate, 4),
            "frames_rendered": self.frames_rendered,
            "wasted_render_rate": round(self.wasted_render_rate, 4),
            "rerenders_after_scrub": self.rerenders_after_scrub,
            "time_to_smooth_s": (
                round(self.time_to_smooth_s, 2) if self.time_to_smooth_s is not None else None
            ),
        }
