"""Simulated editor behaviour for Phase 1 testing and Phase 5 benchmarking.

No video, no Resolve - just a stream of playhead positions that mimic how an
editor moves around a timeline: linear playback, scrubbing, and jumping back
and forth between two hot spots.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterator

from frameforge.timeline import Timeline

SAMPLE_CLIPS = [
    {"name": "A", "start": 0, "end": 300, "cost": 2},
    {"name": "B", "start": 301, "end": 600, "cost": 9},
    {"name": "C", "start": 601, "end": 900, "cost": 5},
]

HARD_TIMELINE = [
    {"name": "intro", "start": 0, "end": 240, "effects": ["Color Correction"]},
    {"name": "mblur_1", "start": 240, "end": 480, "effects": ["Motion Blur"]},
    {"name": "plain", "start": 480, "end": 720, "effects": []},
    {"name": "denoise", "start": 720, "end": 1080, "effects": ["Noise Reduction"]},
    {"name": "fusion_shot", "start": 1080, "end": 1320, "effects": ["Fusion"]},
    {"name": "optflow", "start": 1320, "end": 1560, "effects": ["Optical Flow"]},
    {"name": "stack", "start": 1560, "end": 1920, "effects": ["Motion Blur", "Noise Reduction"]},
]


def sample_timeline() -> Timeline:
    return Timeline.from_dicts(SAMPLE_CLIPS, name="sample")


def hard_timeline(fps: float = 24.0) -> Timeline:
    return Timeline.from_dicts(HARD_TIMELINE, fps=fps, name="hard")


@dataclass
class FakeEditor:
    """Generates playhead traces over a timeline."""

    timeline: Timeline
    seed: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def linear_playback(self, start: float = 0.0, step: float = 1.0) -> Iterator[float]:
        pos = start
        end = self.timeline.duration
        while pos < end:
            yield pos
            pos += step

    def scrub(self, center: float, span: float = 120.0, passes: int = 6) -> Iterator[float]:
        for _ in range(passes):
            for pos in self._linspace(center - span, center + span, 20):
                yield max(0.0, pos)
            for pos in self._linspace(center + span, center - span, 20):
                yield max(0.0, pos)

    def ping_pong(self, a: float, b: float, jumps: int = 10, dwell: int = 8) -> Iterator[float]:
        for i in range(jumps):
            target = a if i % 2 == 0 else b
            for _ in range(dwell):
                yield target + self._rng.uniform(-4, 4)

    def mixed_session(self) -> Iterator[float]:
        yield from self.linear_playback(0, step=3)
        yield from self.scrub(self.timeline.duration * 0.55, span=150, passes=4)
        yield from self.ping_pong(self.timeline.duration * 0.2, self.timeline.duration * 0.8)
        yield from self.linear_playback(self.timeline.duration * 0.4, step=3)

    @staticmethod
    def _linspace(a: float, b: float, n: int) -> Iterator[float]:
        if n <= 1:
            yield a
            return
        step = (b - a) / (n - 1)
        for i in range(n):
            yield a + step * i
