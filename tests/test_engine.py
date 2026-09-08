"""Host protocol and CacheEngine - the integration surface."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import (
    CacheEngine,
    Capability,
    EngineConfig,
    Segment,
    capabilities_of,
    describe_host,
)
from frameforge.host import check_host
from frameforge.timeline import Clip, Timeline


# --------------------------------------------------------------------- hosts
class MinimalHost:
    """The smallest legal host."""

    def __init__(self, delay_per_cost=0.0):
        self.delay_per_cost = delay_per_cost
        self.rendered = []

    def render(self, segment):
        if self.delay_per_cost:
            time.sleep(self.delay_per_cost * segment.cost)
        self.rendered.append(segment.name)


class FullHost(MinimalHost):
    """A host implementing every optional capability."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cache = set()
        self.evicted = []
        self.position = 0.0
        self.hints = {}

    def render(self, segment):
        super().render(segment)
        self.cache.add(segment.name)

    def playhead(self):
        return self.position

    def is_cached(self, segment):
        return segment.name in self.cache

    def evict(self, segment):
        self.cache.discard(segment.name)
        self.evicted.append(segment.name)

    def cost_hint(self, segment):
        return self.hints.get(segment.name)


class BrokenHost(MinimalHost):
    def render(self, segment):
        raise RuntimeError("gpu on fire")


def _timeline():
    return Timeline([
        Clip("cheap", 0, 240, effects=[]),
        Clip("mid", 240, 480, effects=["Blur"]),
        Clip("dear", 480, 720, effects=["Noise Reduction", "Composite"]),
    ])


# ---------------------------------------------------------------- capability
def test_minimal_host_is_accepted():
    assert capabilities_of(MinimalHost()) == {Capability.RENDER}


def test_full_host_reports_everything():
    assert capabilities_of(FullHost()) == set(Capability)


def test_host_without_render_is_rejected():
    with pytest.raises(TypeError, match="render"):
        check_host(object())


def test_describe_host_flags_missing_playhead():
    text = describe_host(MinimalHost())
    assert "MinimalHost" in text and "set_playhead" in text


# -------------------------------------------------------------------- engine
def test_engine_renders_in_priority_order():
    host = MinimalHost()
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(600)          # sitting on the expensive clip
    engine.run(until_complete=True)
    assert host.rendered[0] == "dear"
    assert set(host.rendered) == {"cheap", "mid", "dear"}


def test_engine_stops_when_everything_is_cached():
    engine = CacheEngine(MinimalHost(), _timeline())
    engine.set_playhead(0)
    assert engine.run(until_complete=True) == 3
    assert engine.pending == 0
    assert engine.step() == []


def test_measured_cost_replaces_estimates():
    host = MinimalHost(delay_per_cost=0.004)
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(0)
    engine.run(until_complete=True)
    measured = engine.cost.measured
    assert set(measured) == {"cheap", "mid", "dear"}
    # the segment with two effects really did take longest
    assert max(measured, key=measured.get) == "dear"


def test_engine_pulls_playhead_from_capable_host():
    host = FullHost()
    host.position = 600
    engine = CacheEngine(host, _timeline())
    engine.next_segment()
    assert engine.scheduler.playhead == 600
    host.position = 120
    engine.next_segment()
    assert engine.scheduler.playhead == 120


def test_polled_playhead_changes_the_order():
    # Equal-cost clips, so proximity alone decides and the effect is unambiguous.
    flat = Timeline([Clip("a", 0, 240), Clip("b", 240, 480), Clip("c", 480, 720)])
    host = FullHost()
    engine = CacheEngine(host, flat)
    host.position = 600
    assert engine.next_segment().name == "c"
    host.position = 0
    assert engine.next_segment().name == "a"


def test_push_playhead_is_ignored_when_host_polls():
    host = FullHost()
    host.position = 600
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(0)
    engine.next_segment()             # poll overwrites the pushed value
    assert engine.scheduler.playhead == 600


def test_polling_can_be_disabled():
    host = FullHost()
    host.position = 600
    engine = CacheEngine(host, _timeline(), config=EngineConfig(poll_playhead=False))
    engine.set_playhead(0)
    engine.next_segment()
    assert engine.scheduler.playhead == 0


def test_engine_trusts_host_cache_query():
    host = FullHost()
    host.cache.add("dear")            # host says it already has the expensive one
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(600)
    engine.run(until_complete=True)
    assert "dear" not in host.rendered


def test_cost_hints_seed_the_model():
    host = FullHost()
    host.hints = {"cheap": 99.0}      # host insists the cheap clip is expensive
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(0)
    assert engine.next_segment().name == "cheap"


def test_render_failure_is_recorded_not_raised():
    engine = CacheEngine(BrokenHost(), _timeline())
    engine.set_playhead(0)
    results = engine.step(1)
    assert len(results) == 1
    assert not results[0].ok
    assert "gpu on fire" in results[0].error
    assert engine.stats()["render_failures"] == 1
    # a failed segment stays pending so it can be retried
    assert engine.pending == 3


def test_cache_budget_evicts_and_reschedules():
    host = FullHost()
    engine = CacheEngine(
        host, _timeline(), config=EngineConfig(cache_budget_frames=480)
    )
    engine.set_playhead(0)
    engine.run(max_segments=3)
    assert host.evicted                       # budget forced an eviction
    assert engine.cache.used_frames <= 480


def test_invalidate_range_marks_segments_dirty():
    engine = CacheEngine(MinimalHost(), _timeline())
    engine.set_playhead(0)
    engine.run(until_complete=True)
    assert engine.pending == 0
    assert engine.invalidate_range(300, 500) == 2   # "mid" and "dear" overlap
    assert engine.pending == 2


def test_reload_timeline_keeps_cached_segments():
    engine = CacheEngine(MinimalHost(), _timeline())
    engine.set_playhead(0)
    engine.run(until_complete=True)
    engine.load_timeline(_timeline())
    assert engine.pending == 0


def test_stats_shape():
    engine = CacheEngine(MinimalHost(), _timeline())
    engine.set_playhead(100)
    engine.step(1)
    stats = engine.stats()
    assert stats["segments_total"] == 3
    assert stats["segments_cached"] == 1
    assert stats["renders"] == 1
    assert stats["playhead"] == 100.0


# ------------------------------------------------------------------ threading
def test_background_thread_warms_the_cache():
    host = MinimalHost(delay_per_cost=0.001)
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(0)
    with engine:
        deadline = time.time() + 3.0
        while engine.pending and time.time() < deadline:
            time.sleep(0.01)
    assert engine.pending == 0
    assert engine._thread is None


def test_set_playhead_is_safe_while_rendering():
    host = MinimalHost(delay_per_cost=0.002)
    engine = CacheEngine(host, _timeline())
    engine.set_playhead(0)
    errors = []

    def scrub():
        try:
            for i in range(200):
                engine.set_playhead(i * 3 % 720)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=scrub)
    with engine:
        thread.start()
        thread.join()
        deadline = time.time() + 3.0
        while engine.pending and time.time() < deadline:
            time.sleep(0.01)
    assert not errors
    assert engine.pending == 0


# ------------------------------------------------------------------- segment
def test_segment_helpers():
    seg = Segment("s", 10, 40, fps=30.0, cost=4.0)
    assert seg.length == 30
    assert seg.seconds == pytest.approx(1.0)
    assert list(seg.frames())[:3] == [10, 11, 12]
    assert str(seg) == "s[10:40]"
