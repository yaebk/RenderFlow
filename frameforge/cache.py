"""Cache state model and eviction policy (Phase 4 - "keep them cached longer").

FrameForge does not store pixels; it tracks *which* segments are cached and how
much room is left, so it can decide what to evict when the cache budget is hit.
Eviction favours keeping segments that are expensive, near the playhead, or
frequently revisited - the same signals the scheduler uses to fill the cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CacheEntry:
    job_id: str
    frames: int
    est_cost: float
    last_used: float = 0.0
    hits: int = 0


@dataclass
class CacheState:
    """Tracks cached segments against a frame budget."""

    budget_frames: int = 100_000
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    _clock: int = 0
    hits: int = 0
    misses: int = 0

    # ------------------------------------------------------------------ size
    @property
    def used_frames(self) -> int:
        return sum(e.frames for e in self.entries.values())

    @property
    def free_frames(self) -> int:
        return self.budget_frames - self.used_frames

    def __contains__(self, job_id: str) -> bool:
        return job_id in self.entries

    # --------------------------------------------------------------- access
    def touch(self, job_id: str) -> bool:
        """Register a playback read. Returns True on cache hit."""
        self._clock += 1
        entry = self.entries.get(job_id)
        if entry is None:
            self.misses += 1
            return False
        entry.last_used = self._clock
        entry.hits += 1
        self.hits += 1
        return True

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    # ------------------------------------------------------------------ fill
    def admit(self, job_id: str, frames: int, est_cost: float, keep_score: Callable[[CacheEntry], float]) -> list[str]:
        """Insert a segment, evicting the lowest-value entries to make room.

        ``keep_score`` ranks existing entries; the lowest scorers are dropped
        first.  Returns the list of evicted job ids.
        """
        self._clock += 1
        if job_id in self.entries:
            self.entries[job_id].last_used = self._clock
            return []

        evicted: list[str] = []
        if frames > self.budget_frames:
            return evicted  # segment can never fit; caller should chunk it

        while self.free_frames < frames and self.entries:
            victim_id = min(self.entries, key=lambda k: keep_score(self.entries[k]))
            del self.entries[victim_id]
            evicted.append(victim_id)

        self.entries[job_id] = CacheEntry(job_id, frames, est_cost, self._clock)
        return evicted

    def drop(self, job_id: str) -> None:
        self.entries.pop(job_id, None)
