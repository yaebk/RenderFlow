"""Composite interval flattening (multi-track timelines)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CostEstimator, Scheduler
from frameforge.flatten import composite_intervals, flatten
from frameforge.timeline import Clip, Timeline


def _tl(*clips):
    return Timeline(list(clips))


def test_single_track_is_unchanged():
    tl = _tl(Clip("a", 0, 100), Clip("b", 100, 200))
    ivs = composite_intervals(tl)
    assert [(i.start, i.end) for i in ivs] == [(0, 100), (100, 200)]


def test_gaps_are_dropped():
    tl = _tl(Clip("a", 0, 100), Clip("b", 300, 400))
    ivs = composite_intervals(tl)
    assert [(i.start, i.end) for i in ivs] == [(0, 100), (300, 400)]


def test_overlap_is_split_and_costs_sum():
    # An adjustment layer on T2 covering the middle of a T1 clip.
    tl = _tl(
        Clip("base", 0, 300, effects=["Motion Blur"], track=1),
        Clip("adj", 100, 200, effects=["Noise Reduction"], track=2),
    )
    est = CostEstimator()
    ivs = composite_intervals(tl, est)
    assert [(i.start, i.end) for i in ivs] == [(0, 100), (100, 200), (200, 300)]

    plain, stacked, plain2 = ivs
    # base(1) + motion blur(8) = 9 ; stacked adds noise reduction(10) = 19
    assert plain.cost == pytest.approx(9.0)
    assert stacked.cost == pytest.approx(19.0)
    assert plain2.cost == pytest.approx(9.0)
    assert stacked.cost > plain.cost


def test_stacked_region_is_the_most_expensive_job():
    tl = _tl(
        Clip("base", 0, 300, effects=["Motion Blur"], track=1),
        Clip("adj", 100, 200, effects=["Noise Reduction"], track=2),
        Clip("title", 100, 200, effects=["Composite"], track=3),
    )
    flat = flatten(tl, CostEstimator())
    sched = Scheduler()
    sched.load_timeline(flat)
    costs = {j.name: j.est_cost for j in sched.pending_jobs()}
    assert costs["100-200"] == max(costs.values())


def test_no_duplicate_coverage_of_the_same_frames():
    """Three tracks covering the same frames must not be scheduled three times."""
    tl = _tl(
        Clip("clip", 108202, 108224, effects=["Composite"], track=1),
        Clip("adj_a", 108202, 108235, effects=["Composite"], track=2),
        Clip("adj_b", 108202, 108235, effects=["Composite"], track=3),
    )
    ivs = composite_intervals(tl)
    # Every frame is covered exactly once.
    covered = []
    for iv in ivs:
        covered.extend(range(iv.start, iv.end))
    assert len(covered) == len(set(covered))
    assert min(covered) == 108202 and max(covered) == 108234


def test_interval_reports_tracks_and_effects():
    tl = _tl(
        Clip("base", 0, 200, effects=["Motion Blur"], track=1),
        Clip("adj", 0, 200, effects=["Composite"], track=2),
    )
    iv = composite_intervals(tl)[0]
    assert iv.tracks == (1, 2)
    assert sorted(iv.effects) == ["Composite", "Motion Blur"]
    assert "T1:base" in iv.describe()


def test_min_length_absorbs_slivers():
    # 1-frame solid colours, as editors sprinkle on upper tracks.
    tl = _tl(
        Clip("main", 0, 300, track=1),
        Clip("solid1", 40, 41, track=4),
        Clip("solid2", 80, 81, track=4),
    )
    ivs = composite_intervals(tl, min_length=30)
    assert all(iv.length >= 30 for iv in ivs)
    assert sum(iv.length for iv in ivs) == 300  # nothing lost


def test_min_length_preserves_total_coverage():
    tl = _tl(
        Clip("a", 0, 100, track=1),
        Clip("b", 100, 205, track=1),
        Clip("over", 95, 110, track=2),
    )
    before = composite_intervals(tl, min_length=1)
    after = composite_intervals(tl, min_length=50)
    assert sum(i.length for i in before) == sum(i.length for i in after) == 205
    assert after[0].start == 0 and after[-1].end == 205


def test_merged_cost_is_length_weighted():
    tl = _tl(
        Clip("cheap", 0, 90, track=1),
        Clip("spike", 90, 100, effects=["Noise Reduction"], track=1),
    )
    merged = composite_intervals(tl, min_length=100)
    assert len(merged) == 1
    # (1.0 * 90 + 11.0 * 10) / 100 = 2.0
    assert merged[0].cost == pytest.approx(2.0)


def test_flatten_produces_scheduler_ready_timeline():
    tl = _tl(
        Clip("base", 0, 300, effects=["Motion Blur"], track=1),
        Clip("adj", 100, 200, effects=["Composite"], track=2),
    )
    flat = flatten(tl)
    assert len(flat) == 3
    assert all(c.track == 1 for c in flat)
    sched = Scheduler()
    sched.load_timeline(flat)
    sched.update_playhead(150)
    assert sched.next_job() is not None
    assert len(sched.pending_jobs()) == 3


def test_empty_timeline():
    assert composite_intervals(Timeline([])) == []
    assert len(flatten(Timeline([]))) == 0


def test_measured_profile_overrides_estimates():
    est = CostEstimator()
    clip = Clip("100-200", 100, 200, effects=["Motion Blur"])
    assert est.estimate(clip) == pytest.approx(9.0)
    est.measured["100-200"] = 42.0
    assert est.estimate(clip) == pytest.approx(42.0)


def test_profile_round_trip(tmp_path):
    est = CostEstimator()
    est.measured = {"0-100": 1.5, "100-200": 9.25}
    path = tmp_path / "profile.json"
    est.save_profile(path, timeline="test")
    fresh = CostEstimator()
    fresh.load_profile(path)
    assert fresh.measured == {"0-100": 1.5, "100-200": 9.25}
