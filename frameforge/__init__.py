"""FrameForge - an adaptive render-cache scheduler for any frame renderer.

FrameForge answers one question, over and over, as fast as your cursor moves:

    "Given where the viewer is looking right now, what should I render next?"

It ranks not-yet-cached timeline segments by render cost, distance from the
playhead, playback direction and revisit history, so expensive work the user is
about to need gets cached before cheap work they will never look at.

It is not tied to any editing application.  You supply a host with a ``render``
method; FrameForge decides what to hand it::

    from frameforge import CacheEngine
    from frameforge.formats import load

    class MyHost:
        def render(self, segment):
            my_renderer.render_range(segment.start, segment.end)

    engine = CacheEngine(MyHost(), load("edit.otio"))
    engine.start()
    engine.set_playhead(1200)

See :mod:`frameforge.host` for the full (small) protocol.
"""

from frameforge.timeline import Clip, Timeline
from frameforge.cost import (
    EFFECT_COST,
    CostEstimator,
    normalize_effect_name,
)
from frameforge.flatten import Interval, composite_intervals, flatten
from frameforge.scheduler import RenderJob, Scheduler, SchedulerConfig
from frameforge.cache import CacheState
from frameforge.metrics import Metrics
from frameforge.host import (
    BaseHost,
    Capability,
    RenderHost,
    RenderResult,
    Segment,
    capabilities_of,
    describe_host,
)
from frameforge.engine import CacheEngine, EngineConfig

__version__ = "0.2.0"

__all__ = [
    # timeline
    "Clip",
    "Timeline",
    # cost
    "EFFECT_COST",
    "CostEstimator",
    "normalize_effect_name",
    # flattening
    "Interval",
    "composite_intervals",
    "flatten",
    # scheduling
    "RenderJob",
    "Scheduler",
    "SchedulerConfig",
    "CacheState",
    "Metrics",
    # integration
    "BaseHost",
    "Capability",
    "RenderHost",
    "RenderResult",
    "Segment",
    "capabilities_of",
    "describe_host",
    "CacheEngine",
    "EngineConfig",
]
