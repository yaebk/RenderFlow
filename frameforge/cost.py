"""Effect cost estimation (Phase 3).

Phase 1 uses the static table below.  Later phases replace ``estimate`` with
values measured from real render timings (see
:meth:`frameforge.scheduler.Scheduler.record_render_time`).
"""

from __future__ import annotations

from typing import Mapping, Sequence

from frameforge.timeline import Clip, Timeline

# Static per-effect cost, roughly "relative render time".  Values mirror the
# handoff.  Anything not listed falls back to DEFAULT_EFFECT_COST.
EFFECT_COST: dict[str, float] = {
    "Motion Blur": 8.0,
    "Noise Reduction": 10.0,
    "Optical Flow": 9.0,
    "Fusion": 8.0,
    "Gaussian Blur": 4.0,
    "Color Correction": 2.0,
    "Temporal NR": 12.0,
    "Spatial NR": 6.0,
    "Film Grain": 3.0,
    "Lens Blur": 5.0,
    "Face Refinement": 11.0,
    "Super Scale": 13.0,
}

DEFAULT_EFFECT_COST = 3.0
BASE_CLIP_COST = 1.0  # cost of decoding/playing a clip with no effects


class CostEstimator:
    """Turns a clip's effect list into a scalar render cost.

    A learned multiplier per effect is folded in as render timings arrive, so
    the estimate drifts toward measured reality over a session.
    """

    def __init__(
        self,
        table: Mapping[str, float] | None = None,
        default: float = DEFAULT_EFFECT_COST,
        base: float = BASE_CLIP_COST,
    ) -> None:
        self.table = dict(table or EFFECT_COST)
        self.default = default
        self.base = base
        # effect name -> (sum_ratio, n) of measured/estimated ratios
        self._learned: dict[str, tuple[float, int]] = {}

    def effect_cost(self, effect: str) -> float:
        raw = self.table.get(effect, self.default)
        ratio_sum, n = self._learned.get(effect, (0.0, 0))
        if n:
            raw *= ratio_sum / n
        return raw

    def estimate(self, clip: Clip) -> float:
        if clip.cost is not None and not clip.effects:
            return float(clip.cost)
        cost = self.base + sum(self.effect_cost(e) for e in clip.effects)
        if clip.cost is not None:
            # Explicit cost acts as a floor / manual override hint.
            cost = max(cost, float(clip.cost))
        return cost

    def estimate_timeline(self, timeline: Timeline) -> dict[str, float]:
        return {clip.name: self.estimate(clip) for clip in timeline}

    def observe(self, effects: Sequence[str], measured_cost: float, estimated_cost: float) -> None:
        """Fold a measured render cost back into the per-effect multipliers."""
        if estimated_cost <= 0 or not effects:
            return
        ratio = measured_cost / estimated_cost
        for effect in effects:
            ratio_sum, n = self._learned.get(effect, (0.0, 0))
            self._learned[effect] = (ratio_sum + ratio, n + 1)
