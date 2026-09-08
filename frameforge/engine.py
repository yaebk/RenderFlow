"""``CacheEngine`` - the thing you actually use.

Wires a :class:`~frameforge.scheduler.Scheduler` to a host and keeps the cache
warm.  Typical use from an application with its own event loop::

    engine = CacheEngine(host, timeline)
    engine.start()                      # background warming thread
    ...
    engine.set_playhead(frame)          # call from your UI/playback callback
    ...
    engine.stop()

Or drive it yourself, one segment at a time::

    engine.set_playhead(frame)
    engine.step()

Every render is timed and fed back into the cost model, so the scheduler's idea
of "expensive" converges on what your renderer actually does.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from frameforge.cache import CacheState
from frameforge.cost import CostEstimator
from frameforge.flatten import flatten
from frameforge.host import (
    Capability,
    RenderResult,
    Segment,
    capabilities_of,
    check_host,
)
from frameforge.metrics import Metrics
from frameforge.scheduler import RenderJob, Scheduler, SchedulerConfig
from frameforge.timeline import Clip, Timeline


@dataclass
class EngineConfig:
    #: Composite segments shorter than this are merged into neighbours.
    min_segment_frames: int = 1
    #: Cache budget in frames. ``None`` disables budgeting entirely.
    cache_budget_frames: int | None = None
    #: Seconds to wait when there is nothing left to render.
    idle_sleep: float = 0.1
    #: Re-read the host playhead before every render decision.
    poll_playhead: bool = True


class CacheEngine:
    def __init__(
        self,
        host: object,
        timeline: Timeline | None = None,
        *,
        config: EngineConfig | None = None,
        scheduler_config: SchedulerConfig | None = None,
        estimator: CostEstimator | None = None,
    ) -> None:
        self.capabilities = check_host(host)
        self.host = host
        self.config = config or EngineConfig()
        self.cost = estimator or CostEstimator()
        self.scheduler = Scheduler(scheduler_config, self.cost)
        self.metrics = Metrics()
        self.cache = CacheState(
            budget_frames=self.config.cache_budget_frames
            if self.config.cache_budget_frames is not None
            else 10**9
        )

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._segments: dict[str, Segment] = {}
        self._fps = 24.0
        self.results: list[RenderResult] = []

        if timeline is not None:
            self.load_timeline(timeline)

    # ------------------------------------------------------------- timeline
    def load_timeline(self, timeline: Timeline) -> None:
        """(Re)load the timeline. Cached segments that still exist stay cached."""
        with self._lock:
            self._fps = timeline.fps
            flat = flatten(timeline, self.cost, self.config.min_segment_frames)
            self.scheduler.load_timeline(flat)
            self._segments = {
                job.id: Segment(
                    name=job.name,
                    start=job.start,
                    end=job.end,
                    fps=timeline.fps,
                    cost=job.est_cost,
                    effects=job.effects,
                )
                for job in self.scheduler._jobs.values()
            }
            if Capability.COST_HINT in self.capabilities:
                self._apply_cost_hints()

    def _apply_cost_hints(self) -> None:
        """Seed the cost model from the host, then let measurement overwrite it."""
        for segment in self._segments.values():
            hint = self.host.cost_hint(segment)
            if hint is not None:
                self.cost.measured.setdefault(segment.name, float(hint))
        hinted = Timeline(
            [
                Clip(s.name, s.start, s.end, s.effects, self.cost.measured.get(s.name))
                for s in self._segments.values()
            ],
            fps=self._fps,
        )
        self.scheduler.load_timeline(hinted)

    def invalidate_range(self, start: int, end: int) -> int:
        """Mark every segment overlapping [start, end) as needing a re-render."""
        with self._lock:
            n = 0
            for job in list(self.scheduler._jobs.values()):
                if job.start < end and start < job.end:
                    self.scheduler.invalidate(job.id)
                    self.cache.drop(job.id)
                    n += 1
            return n

    # ------------------------------------------------------------- playhead
    def set_playhead(self, position: float) -> None:
        """Push the current cursor position. Safe to call from another thread."""
        with self._lock:
            self.scheduler.update_playhead(position)

    def _refresh_playhead(self) -> None:
        if self.config.poll_playhead and Capability.PLAYHEAD in self.capabilities:
            pos = self.host.playhead()
            if pos is not None:
                self.scheduler.update_playhead(float(pos))

    # --------------------------------------------------------------- render
    def next_segment(self) -> Segment | None:
        """What the engine would render next, without rendering it."""
        with self._lock:
            self._refresh_playhead()
            job = self._pick()
            return self._segments.get(job.id) if job else None

    def _pick(self) -> RenderJob | None:
        job = self.scheduler.next_job()
        while job is not None and Capability.CACHE_QUERY in self.capabilities:
            segment = self._segments.get(job.id)
            if segment is None or not self.host.is_cached(segment):
                break
            # The host already has it - trust the host, take the next one.
            self.scheduler.mark_cached(job.id, True)
            job = self.scheduler.next_job()
        return job

    def step(self, n: int = 1) -> list[RenderResult]:
        """Render the ``n`` highest-priority segments. Blocking."""
        out: list[RenderResult] = []
        for _ in range(n):
            with self._lock:
                self._refresh_playhead()
                job = self._pick()
                if job is None:
                    break
                segment = self._segments[job.id]

            # Render outside the lock so set_playhead() stays responsive.
            t0 = time.perf_counter()
            error = None
            try:
                self.host.render(segment)
            except Exception as exc:  # noqa: BLE001 - a bad segment must not kill the loop
                error = f"{type(exc).__name__}: {exc}"
            elapsed = time.perf_counter() - t0

            result = RenderResult(segment, elapsed, ok=error is None, error=error)
            out.append(result)

            with self._lock:
                self.results.append(result)
                self.metrics.record_render(used=True)
                if result.ok:
                    self.scheduler.mark_cached(job.id, True)
                    # Measured cost beats every estimate.
                    self.scheduler.record_render_time(job, elapsed)
                    self.cost.measured[segment.name] = elapsed
                    self._admit(job, segment)
        return out

    def _admit(self, job: RenderJob, segment: Segment) -> None:
        if self.config.cache_budget_frames is None:
            self.cache.admit(job.id, segment.length, job.est_cost, lambda e: 0.0)
            return
        evicted = self.cache.admit(
            job.id, segment.length, job.est_cost, self._keep_score
        )
        for job_id in evicted:
            self.scheduler.invalidate(job_id)
            dropped = self._segments.get(job_id)
            if dropped is not None and Capability.EVICT in self.capabilities:
                self.host.evict(dropped)

    def _keep_score(self, entry) -> float:
        """Keep expensive, recently used, frequently revisited segments."""
        job = self.scheduler._jobs.get(entry.job_id)
        revisits = self.scheduler._visit_score.get(entry.job_id, 0.0) if job else 0.0
        return entry.est_cost * 0.5 + entry.last_used * 0.01 + revisits

    # ------------------------------------------------------------ run loops
    def run(
        self,
        max_seconds: float | None = None,
        max_segments: int | None = None,
        until_complete: bool = False,
    ) -> int:
        """Render in priority order until a stopping condition is hit."""
        deadline = time.time() + max_seconds if max_seconds else None
        rendered = 0
        while not self._stop.is_set():
            if deadline and time.time() >= deadline:
                break
            if max_segments and rendered >= max_segments:
                break
            done = self.step(1)
            if not done:
                if until_complete or deadline is None:
                    break
                time.sleep(self.config.idle_sleep)
                continue
            rendered += len(done)
        return rendered

    def start(self) -> None:
        """Warm the cache on a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._background, name="frameforge-cache", daemon=True
        )
        self._thread.start()

    def _background(self) -> None:
        while not self._stop.is_set():
            if not self.step(1):
                self._stop.wait(self.config.idle_sleep)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        self._thread = None

    def __enter__(self) -> "CacheEngine":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- stats
    @property
    def pending(self) -> int:
        return len(self.scheduler.pending_jobs())

    @property
    def cached(self) -> int:
        return len(self._segments) - self.pending

    def stats(self) -> dict:
        ok = [r for r in self.results if r.ok]
        total = sum(r.seconds for r in ok)
        return {
            "segments_total": len(self._segments),
            "segments_cached": self.cached,
            "segments_pending": self.pending,
            "renders": len(self.results),
            "render_failures": sum(1 for r in self.results if not r.ok),
            "render_seconds": round(total, 3),
            "frames_cached": self.cache.used_frames,
            "cache_hit_rate": round(self.cache.hit_rate, 4),
            "playhead": self.scheduler.playhead,
            "direction": self.scheduler.direction,
        }
