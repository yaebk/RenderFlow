"""Adaptive render-cache scheduler (Phases 1 & 4).

The scheduler holds a set of pending :class:`RenderJob` s (one per timeline
segment that is not yet cached) and answers a single question repeatedly:

    "Given where the editor is looking right now, what should I cache next?"

Priority combines four signals from the handoff::

    P(c) = w_cost * C + w_prox * D + w_dir * V + w_hist * H

* ``C`` - normalised render cost (expensive work is worth caching).
* ``D`` - proximity to the playhead (near work is needed sooner).
* ``V`` - agreement with playback direction (cache ahead of the play cursor).
* ``H`` - revisit history (sections the editor keeps returning to).

Because every one of those signals moves as the editor works, the heap is
rebuilt lazily whenever the playhead (or cache state) changes.
"""

from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from frameforge.cost import CostEstimator
from frameforge.timeline import Clip, Timeline


@dataclass
class SchedulerConfig:
    w_cost: float = 1.0
    w_prox: float = 1.4
    w_dir: float = 0.8
    w_hist: float = 1.1
    # Frames beyond which proximity/direction signals have largely decayed.
    horizon_frames: float = 480.0
    # Weight kept for a segment that lies behind the playback direction.
    behind_direction_weight: float = 0.25
    # Exponential decay applied to a section's revisit score each playhead move.
    history_decay: float = 0.92
    # How many playhead samples define the current direction estimate.
    direction_window: int = 6


@dataclass(order=True)
class _HeapEntry:
    neg_priority: float
    seq: int
    job_id: str = field(compare=False)


@dataclass
class RenderJob:
    """A unit of cache work: one timeline segment on one track."""

    id: str
    name: str
    start: int
    end: int
    track: int
    est_cost: float
    effects: tuple[str, ...] = ()
    cached: bool = False
    # Filled once the job has actually been rendered.
    measured_cost: float | None = None
    priority: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    @property
    def length(self) -> int:
        return self.end - self.start

    def distance_to(self, frame: float) -> float:
        if self.start <= frame < self.end:
            return 0.0
        if frame < self.start:
            return self.start - frame
        return frame - (self.end - 1)

    @classmethod
    def from_clip(cls, clip: Clip, est_cost: float) -> "RenderJob":
        return cls(
            id=f"{clip.track}:{clip.name}:{clip.start}",
            name=clip.name,
            start=clip.start,
            end=clip.end,
            track=clip.track,
            est_cost=est_cost,
            effects=tuple(clip.effects),
        )


class Scheduler:
    def __init__(
        self,
        config: SchedulerConfig | None = None,
        cost_estimator: CostEstimator | None = None,
    ) -> None:
        self.config = config or SchedulerConfig()
        self.cost = cost_estimator or CostEstimator()

        self._jobs: dict[str, RenderJob] = {}
        self._heap: list[_HeapEntry] = []
        self._heap_dirty = True
        self._seq = itertools.count()

        self.playhead: float = 0.0
        self._playhead_history: list[float] = []
        self.direction: int = 0  # -1 back, 0 idle, +1 forward
        # segment id -> revisit score
        self._visit_score: dict[str, float] = {}
        self._max_cost: float = 1.0

    # ------------------------------------------------------------------ jobs
    def load_timeline(self, timeline: Timeline, keep_cache: bool = True) -> None:
        """(Re)build the job set from a timeline, preserving cache flags."""
        cached_ids = {j.id for j in self._jobs.values() if j.cached} if keep_cache else set()
        self._jobs.clear()
        for clip in timeline:
            job = RenderJob.from_clip(clip, self.cost.estimate(clip))
            job.cached = job.id in cached_ids
            self._jobs[job.id] = job
        self._max_cost = max((j.est_cost for j in self._jobs.values()), default=1.0)
        self._heap_dirty = True

    def add_job(self, job: RenderJob) -> None:
        self._jobs[job.id] = job
        self._max_cost = max(self._max_cost, job.est_cost)
        self._heap_dirty = True

    def pending_jobs(self) -> list[RenderJob]:
        return [j for j in self._jobs.values() if not j.cached]

    def mark_cached(self, job_id: str, cached: bool = True) -> None:
        job = self._jobs.get(job_id)
        if job and job.cached != cached:
            job.cached = cached
            self._heap_dirty = True

    def invalidate(self, job_id: str) -> None:
        """A segment changed (edit, effect tweak) - it needs re-rendering."""
        self.mark_cached(job_id, False)

    # -------------------------------------------------------------- playhead
    def update_playhead(self, position: float) -> None:
        prev = self.playhead
        self.playhead = float(position)

        hist = self._playhead_history
        hist.append(self.playhead)
        del hist[: -self.config.direction_window]
        if len(hist) >= 2:
            delta = hist[-1] - hist[0]
            self.direction = (delta > 0) - (delta < 0)

        # Decay every section's revisit score, then bump the one under the head.
        decay = self.config.history_decay
        for key in list(self._visit_score):
            self._visit_score[key] *= decay
            if self._visit_score[key] < 1e-3:
                del self._visit_score[key]

        if prev != self.playhead:
            for job in self._jobs.values():
                if job.distance_to(self.playhead) == 0.0:
                    self._visit_score[job.id] = self._visit_score.get(job.id, 0.0) + 1.0

        self._heap_dirty = True

    # -------------------------------------------------------------- priority
    def calculate_priority(self, job: RenderJob) -> float:
        cfg = self.config
        horizon = cfg.horizon_frames

        # C - normalised render cost
        c = job.est_cost / self._max_cost if self._max_cost else 0.0

        dist = job.distance_to(self.playhead)

        # D - proximity, 1 at the playhead decaying to ~0 by the horizon
        d = 1.0 / (1.0 + dist / horizon)

        # V - playback-direction agreement
        if self.direction == 0:
            v = d  # idle: symmetric, mirrors proximity
        else:
            ahead = (job.midpoint - self.playhead) * self.direction > 0
            reach = max(0.0, 1.0 - dist / (horizon * 1.5))
            v = reach if ahead else cfg.behind_direction_weight * reach

        # H - revisit history for this section
        raw_hist = self._visit_score.get(job.id, 0.0)
        h = raw_hist / (1.0 + raw_hist)  # squashed to [0, 1)

        job.priority = cfg.w_cost * c + cfg.w_prox * d + cfg.w_dir * v + cfg.w_hist * h
        return job.priority

    def _rebuild_heap(self) -> None:
        self._heap = []
        for job in self._jobs.values():
            if job.cached:
                continue
            entry = _HeapEntry(-self.calculate_priority(job), next(self._seq), job.id)
            heapq.heappush(self._heap, entry)
        self._heap_dirty = False

    # ------------------------------------------------------------------ pull
    def next_job(self) -> RenderJob | None:
        """Highest-priority pending job, or ``None`` if everything is cached."""
        if self._heap_dirty:
            self._rebuild_heap()
        while self._heap:
            entry = heapq.heappop(self._heap)
            job = self._jobs.get(entry.job_id)
            if job and not job.cached:
                heapq.heappush(self._heap, entry)  # keep it; caller decides when cached
                return job
        return None

    def priority_order(self) -> list[RenderJob]:
        """All pending jobs, most valuable to cache first."""
        return sorted(self.pending_jobs(), key=self.calculate_priority, reverse=True)

    def take(self, n: int = 1) -> list[RenderJob]:
        """Pop up to ``n`` jobs and mark them cached (simulates dispatch)."""
        out: list[RenderJob] = []
        for _ in range(n):
            job = self.next_job()
            if job is None:
                break
            self.mark_cached(job.id, True)
            out.append(job)
        return out

    # -------------------------------------------------------------- learning
    def record_render_time(self, job: RenderJob | str, seconds: float) -> None:
        job = self._jobs[job] if isinstance(job, str) else job
        job.measured_cost = seconds
        self.cost.observe(job.effects, seconds, job.est_cost)
        # Refresh this job's estimate with the blended multiplier.
        job.est_cost = self.cost.estimate(
            Clip(job.name, job.start, job.end, job.effects, track=job.track)
        )
        self._max_cost = max((j.est_cost for j in self._jobs.values()), default=1.0)
        self._heap_dirty = True

    # ----------------------------------------------------------------- debug
    def explain(self, job: RenderJob) -> dict[str, float]:
        cfg = self.config
        dist = job.distance_to(self.playhead)
        c = job.est_cost / self._max_cost if self._max_cost else 0.0
        d = 1.0 / (1.0 + dist / cfg.horizon_frames)
        self.calculate_priority(job)
        return {
            "distance": dist,
            "C": c,
            "D": d,
            "history": self._visit_score.get(job.id, 0.0),
            "priority": job.priority,
        }
