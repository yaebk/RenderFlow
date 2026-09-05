"""Timeline data model.

A :class:`Timeline` is an ordered list of :class:`Clip` segments measured in
frames.  This is the neutral representation the scheduler consumes; both the
simulated timeline and the Resolve adapter produce it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence


@dataclass
class Clip:
    """A contiguous timeline segment.

    Attributes
    ----------
    name:
        Human readable identifier (Resolve clip name, or "A"/"B"/... in tests).
    start, end:
        Inclusive/exclusive frame bounds on the timeline.  ``end`` is the first
        frame *after* the clip.
    effects:
        Names of effects applied to the clip.  Used by :class:`CostEstimator`
        when an explicit ``cost`` is not supplied.
    cost:
        Optional pre-computed render cost.  When ``None`` the cost estimator
        derives it from ``effects``.
    track:
        Video track index the clip lives on (1-based, matching Resolve).
    """

    name: str
    start: int
    end: int
    effects: Sequence[str] = field(default_factory=tuple)
    cost: float | None = None
    track: int = 1

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"clip {self.name!r}: end ({self.end}) must be > start ({self.start})")
        self.effects = tuple(self.effects)

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    def contains(self, frame: float) -> bool:
        return self.start <= frame < self.end

    def distance_to(self, frame: float) -> float:
        """Frames between ``frame`` and the nearest edge of the clip (0 if inside)."""
        if self.contains(frame):
            return 0.0
        if frame < self.start:
            return self.start - frame
        return frame - (self.end - 1)


@dataclass
class Timeline:
    """Ordered collection of clips plus timeline-wide metadata."""

    clips: list[Clip]
    fps: float = 24.0
    name: str = "timeline"

    def __post_init__(self) -> None:
        self.clips = sorted(self.clips, key=lambda c: (c.track, c.start))

    def __iter__(self) -> Iterator[Clip]:
        return iter(self.clips)

    def __len__(self) -> int:
        return len(self.clips)

    @property
    def duration(self) -> int:
        return max((c.end for c in self.clips), default=0)

    def clip_at(self, frame: float, track: int = 1) -> Clip | None:
        for clip in self.clips:
            if clip.track == track and clip.contains(frame):
                return clip
        return None

    def seconds_to_frames(self, seconds: float) -> float:
        return seconds * self.fps

    @classmethod
    def from_dicts(cls, rows: Iterable[dict], **kwargs) -> "Timeline":
        """Build a timeline from the plain-dict form used in the handoff."""
        clips = [
            Clip(
                name=r["name"],
                start=int(r["start"]),
                end=int(r["end"]),
                effects=tuple(r.get("effects", ())),
                cost=r.get("cost"),
                track=int(r.get("track", 1)),
            )
            for r in rows
        ]
        return cls(clips=clips, **kwargs)
