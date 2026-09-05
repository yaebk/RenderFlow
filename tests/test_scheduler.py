"""Core scheduler behaviour (FLAG 1, FLAG 8, FLAG 9)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CostEstimator, Scheduler, SchedulerConfig
from frameforge.cache import CacheState
from frameforge.simulation import hard_timeline, sample_timeline
from frameforge.timeline import Clip, Timeline


def test_handoff_example_order():
    sched = Scheduler(SchedulerConfig(w_cost=2.0, w_prox=1.0, w_dir=0.0, w_hist=0.0))
    sched.load_timeline(sample_timeline())
    sched.update_playhead(450)
    assert [j.name for j in sched.priority_order()] == ["B", "C", "A"]


def test_next_job_matches_priority_order():
    sched = Scheduler()
    sched.load_timeline(hard_timeline())
    sched.update_playhead(600)
    assert sched.next_job().name == sched.priority_order()[0].name


def test_cached_jobs_are_skipped():
    sched = Scheduler()
    sched.load_timeline(sample_timeline())
    sched.update_playhead(0)
    first = sched.take(1)[0]
    assert first.cached
    assert first.name not in [j.name for j in sched.priority_order()]
    assert len(sched.pending_jobs()) == 2


def test_take_drains_in_priority_then_empties():
    sched = Scheduler()
    sched.load_timeline(sample_timeline())
    sched.update_playhead(200)
    taken = sched.take(10)
    assert len(taken) == 3
    assert sched.next_job() is None


def test_direction_changes_priority():
    sched = Scheduler(SchedulerConfig(w_cost=0.0, w_prox=0.5, w_dir=2.0, w_hist=0.0))
    sched.load_timeline(hard_timeline())
    for pos in range(400, 640, 20):  # moving forward, playhead ~620
        sched.update_playhead(pos)
    fwd_top = sched.priority_order()[0]
    assert fwd_top.start >= 480  # something ahead of the playhead

    for pos in range(620, 380, -20):  # now scrubbing backward
        sched.update_playhead(pos)
    back_top = sched.priority_order()[0]
    assert back_top.start <= fwd_top.start


def test_revisit_history_raises_priority():
    sched = Scheduler(SchedulerConfig(w_cost=0.0, w_prox=0.1, w_dir=0.0, w_hist=3.0))
    sched.load_timeline(hard_timeline())
    # Bounce on the 'denoise' clip (720-1080).
    for _ in range(8):
        sched.update_playhead(900)
        sched.update_playhead(905)
    top = sched.priority_order()[0]
    assert top.start <= 900 < top.end


def test_record_render_time_updates_estimate():
    sched = Scheduler()
    tl = Timeline([Clip("x", 0, 100, effects=["Motion Blur"])])
    sched.load_timeline(tl)
    job = sched.pending_jobs()[0]
    before = job.est_cost
    sched.record_render_time(job, before * 3)  # measured way more expensive
    assert job.est_cost > before


def test_load_timeline_preserves_cache_flags():
    sched = Scheduler()
    sched.load_timeline(sample_timeline())
    sched.update_playhead(0)
    sched.take(1)
    cached_names = {j.name for j in sched._jobs.values() if j.cached}
    sched.load_timeline(sample_timeline())
    assert {j.name for j in sched._jobs.values() if j.cached} == cached_names


def test_invalidate_reschedules():
    sched = Scheduler()
    sched.load_timeline(sample_timeline())
    sched.update_playhead(0)
    job = sched.take(1)[0]
    sched.invalidate(job.id)
    assert job.name in [j.name for j in sched.priority_order()]


# ---------------------------------------------------------------- cache state
def test_cache_evicts_lowest_value_first():
    cache = CacheState(budget_frames=300)
    cache.admit("a", 100, est_cost=1.0, keep_score=lambda e: e.est_cost)
    cache.admit("b", 100, est_cost=9.0, keep_score=lambda e: e.est_cost)
    cache.admit("c", 100, est_cost=5.0, keep_score=lambda e: e.est_cost)
    evicted = cache.admit("d", 100, est_cost=7.0, keep_score=lambda e: e.est_cost)
    assert evicted == ["a"]  # cheapest to recompute is dropped
    assert "d" in cache and "a" not in cache


def test_cache_hit_rate():
    cache = CacheState()
    cache.admit("a", 10, 1.0, keep_score=lambda e: 0)
    assert cache.touch("a") is True
    assert cache.touch("b") is False
    assert cache.hit_rate == pytest.approx(0.5)


def test_estimator_learns_toward_measurement():
    est = CostEstimator()
    clip = Clip("m", 0, 10, effects=["Motion Blur"])
    base = est.estimate(clip)
    for _ in range(5):
        est.observe(["Motion Blur"], measured_cost=base * 2, estimated_cost=base)
    assert est.estimate(clip) > base
