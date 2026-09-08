"""The host protocol - FrameForge's entire integration surface.

FrameForge decides *what to render next*.  It never renders anything itself and
knows nothing about video, codecs, or any particular application.  To use it,
you provide a host: an object that can render one segment of your timeline.

The only required method is ``render``::

    class MyHost:
        def render(self, segment):
            my_renderer.render_range(segment.start, segment.end)

Everything else is optional, and FrameForge adapts to what you actually have.
Implement more methods and it schedules better:

======================  ====================================================
``render(segment)``     REQUIRED. Render and cache one segment. Blocking.
``playhead()``          Current cursor position in frames. If absent, push it
                        yourself with ``CacheEngine.set_playhead()``.
``is_cached(segment)``  Whether the segment is already cached, so FrameForge
                        can trust your cache instead of its own bookkeeping.
``cost_hint(segment)``  Your own cost estimate. FrameForge starts from this,
                        then replaces it with what it measures.
``evict(segment)``      Drop a segment. Lets FrameForge enforce a cache budget.
======================  ====================================================

Because *FrameForge calls your renderer*, it times every render and learns the
real cost of each segment for free.  That sidesteps the problem that sinks
plugin-style approaches: no editing application reliably reports what effects
are on a clip or how expensive they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Capability(str, Enum):
    """What a given host can actually do."""

    RENDER = "render"            # required; everything else is a bonus
    PLAYHEAD = "playhead"
    CACHE_QUERY = "is_cached"
    COST_HINT = "cost_hint"
    EVICT = "evict"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Segment:
    """A contiguous frame range handed to the host to render.

    Frame-based and codec-agnostic on purpose: ``start``/``end`` mean whatever
    your renderer means by a frame index.
    """

    name: str
    start: int
    end: int
    fps: float = 24.0
    cost: float = 1.0
    effects: tuple[str, ...] = ()

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def seconds(self) -> float:
        return self.length / self.fps if self.fps else 0.0

    def frames(self) -> range:
        return range(self.start, self.end)

    def __str__(self) -> str:
        return f"{self.name}[{self.start}:{self.end}]"


@dataclass
class RenderResult:
    """Outcome of one ``host.render`` call."""

    segment: Segment
    seconds: float
    ok: bool = True
    error: str | None = None

    @property
    def seconds_per_frame(self) -> float:
        return self.seconds / self.segment.length if self.segment.length else 0.0


@runtime_checkable
class RenderHost(Protocol):
    """Minimal contract. Only ``render`` is required."""

    def render(self, segment: Segment) -> None:  # pragma: no cover - protocol
        ...


class BaseHost:
    """Optional convenience base class. Subclass and override ``render``.

    Using it is never required - any object with a ``render`` method works.
    """

    def render(self, segment: Segment) -> None:
        raise NotImplementedError("a host must implement render(segment)")


def capabilities_of(host: object) -> set[Capability]:
    """Report what a host supports, so callers get no silent surprises."""
    found: set[Capability] = set()
    if callable(getattr(host, "render", None)):
        found.add(Capability.RENDER)
    for cap, attr in (
        (Capability.PLAYHEAD, "playhead"),
        (Capability.CACHE_QUERY, "is_cached"),
        (Capability.COST_HINT, "cost_hint"),
        (Capability.EVICT, "evict"),
    ):
        if callable(getattr(host, attr, None)):
            found.add(cap)
    return found


def check_host(host: object) -> set[Capability]:
    """Like :func:`capabilities_of`, but raises if the host is unusable."""
    caps = capabilities_of(host)
    if Capability.RENDER not in caps:
        raise TypeError(
            f"{type(host).__name__} cannot be used as a FrameForge host: it needs a "
            "render(segment) method. See frameforge.host for the full protocol."
        )
    return caps


def describe_host(host: object) -> str:
    """Human-readable capability summary, handy in logs and startup banners."""
    caps = capabilities_of(host)
    lines = [f"host: {type(host).__name__}"]
    for cap in Capability:
        mark = "yes" if cap in caps else " no"
        lines.append(f"  [{mark}] {cap.value}")
    if Capability.PLAYHEAD not in caps:
        lines.append("  note: no playhead() - push positions with set_playhead()")
    if Capability.EVICT not in caps:
        lines.append("  note: no evict() - cache budget is advisory only")
    return "\n".join(lines)
