"""Flatten a multi-track timeline into non-overlapping composite intervals.

The render cache works on the *composite* image at each timeline frame, not on
individual clips.  Real timelines stack adjustment layers, titles and mattes
over the base video on higher tracks, so scheduling per-clip means:

* the same frame range gets scheduled several times (once per track), and
* a stacked region - the expensive case - reads as several cheap jobs instead
  of one expensive one.

``composite_intervals`` cuts the timeline at the union of every clip boundary
across all tracks and sums the cost of everything covering each slice::

    T1  ────────[ clip A ]──────[ clip B ]────
    T2  ─────[ adjustment layer ]────────────
    ->  │  A  │  A+adj  │ adj │  B  │   B   │

Cost of an interval = base + the "above baseline" cost of every clip covering
it, so a region under three effect-carrying layers is genuinely three times as
expensive to render.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from frameforge.cost import CostEstimator
from frameforge.timeline import Clip, Timeline


@dataclass
class Interval:
    """One non-overlapping slice of the flattened timeline."""

    start: int
    end: int
    sources: tuple[Clip, ...]
    cost: float
    #: Stable identifier. Defaults to the frame range; ``composite_intervals``
    #: upgrades it to the source clip's name where that stays unique.
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.range_name

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def range_name(self) -> str:
        return f"{self.start}-{self.end}"

    def preferred_name(self) -> str:
        """The source clip's name when this interval is exactly one whole clip."""
        if len(self.sources) == 1:
            clip = self.sources[0]
            if clip.start == self.start and clip.end == self.end:
                return clip.name
        return self.range_name

    @property
    def tracks(self) -> tuple[int, ...]:
        return tuple(sorted({c.track for c in self.sources}))

    @property
    def effects(self) -> tuple[str, ...]:
        """All effects covering this interval, with multiplicity."""
        return tuple(e for c in self.sources for e in c.effects)

    def describe(self) -> str:
        srcs = ", ".join(f"T{c.track}:{c.name}" for c in self.sources)
        return f"[{self.start}-{self.end}] len={self.length:<5} cost~{self.cost:5.1f}  <- {srcs}"


def _contribution(clip: Clip, est: CostEstimator) -> float:
    """How much this clip adds above the per-frame baseline."""
    return max(0.0, est.estimate(clip) - est.base)


def _merge(group: list[Interval]) -> Interval:
    """Merge contiguous intervals, cost = length-weighted mean."""
    if len(group) == 1:
        return group[0]
    total_len = sum(iv.length for iv in group) or 1
    cost = sum(iv.cost * iv.length for iv in group) / total_len

    sources: list[Clip] = []
    seen: set[int] = set()
    for iv in group:
        for clip in iv.sources:
            if id(clip) not in seen:
                seen.add(id(clip))
                sources.append(clip)

    return Interval(group[0].start, group[-1].end, tuple(sources), cost)


def _coalesce(intervals: list[Interval], min_length: int) -> list[Interval]:
    """Absorb slivers (1-frame solids, tiny overlaps) into their neighbours."""
    if min_length <= 1 or not intervals:
        return intervals

    runs: list[list[Interval]] = []
    current = [intervals[0]]
    for iv in intervals[1:]:
        if iv.start == current[-1].end:
            current.append(iv)
        else:
            runs.append(current)
            current = [iv]
    runs.append(current)

    out: list[Interval] = []
    for run in runs:
        buf: list[Interval] = []
        for iv in run:
            buf.append(iv)
            if sum(x.length for x in buf) >= min_length:
                out.append(_merge(buf))
                buf = []
        if buf:
            if out and out[-1].end == buf[0].start:
                out[-1] = _merge([out[-1]] + buf)
            else:
                out.append(_merge(buf))
    return out


def composite_intervals(
    timeline: Timeline,
    estimator: CostEstimator | None = None,
    min_length: int = 1,
) -> list[Interval]:
    """Cut ``timeline`` into non-overlapping, cost-summed intervals."""
    est = estimator or CostEstimator()
    if not len(timeline):
        return []

    bounds = sorted({c.start for c in timeline} | {c.end for c in timeline})
    raw: list[Interval] = []
    for a, b in zip(bounds, bounds[1:]):
        covering = tuple(c for c in timeline if c.start <= a and c.end >= b)
        if not covering:
            continue  # gap in the timeline - nothing to render
        cost = est.base + sum(_contribution(c, est) for c in covering)
        raw.append(Interval(a, b, covering, cost))

    intervals = _coalesce(raw, min_length)
    _assign_names(intervals)
    return intervals


def _assign_names(intervals: list[Interval]) -> None:
    """Prefer the source clip's name, but only where it stays unambiguous.

    Editors happily reuse names ("Adjustment Clip" a dozen times), so a name is
    only adopted when exactly one interval wants it; everything else keeps its
    frame range, which is unique by construction.
    """
    counts = Counter(iv.preferred_name() for iv in intervals)
    for interval in intervals:
        preferred = interval.preferred_name()
        interval.name = preferred if counts[preferred] == 1 else interval.range_name


def flatten(
    timeline: Timeline,
    estimator: CostEstimator | None = None,
    min_length: int = 1,
) -> Timeline:
    """``composite_intervals`` wrapped back into a Timeline the Scheduler eats.

    Each interval becomes a single-track clip with an explicit composite cost.
    Effects are carried through (with multiplicity) so measured render times can
    still be attributed back to individual effects.
    """
    intervals = composite_intervals(timeline, estimator, min_length)
    clips = [
        Clip(
            name=iv.name,
            start=iv.start,
            end=iv.end,
            effects=iv.effects,
            cost=iv.cost,
            track=1,
        )
        for iv in intervals
    ]
    return Timeline(clips=clips, fps=timeline.fps, name=f"{timeline.name} (flattened)")
