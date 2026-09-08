"""Render cost estimation.

Cost has three sources, in descending order of trust:

1. **Measured** - what a render actually took.  Because FrameForge drives the
   renderer through :class:`~frameforge.host.RenderHost`, it times every render
   and learns real costs for free.  This is the one that matters.
2. **Explicit** - a cost your application already knows and set on the clip.
3. **Estimated** - the static table below, keyed on effect names.

Only (3) depends on knowing which effects are on a clip, and effect names are
the least portable thing in editorial interchange.  So (3) is a cold-start
guess: good enough to order the first few renders, then overwritten by (1).

Effect names are normalised to a host-neutral vocabulary by
:func:`normalize_effect_name`, so "Fusion", "Precomp", "Nested Sequence" and
"Compound Clip" all price as a composite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from frameforge.timeline import Clip, Timeline

#: Host-neutral effect vocabulary, priced as rough multiples of a plain clip.
EFFECT_COST: dict[str, float] = {
    "Temporal Noise Reduction": 12.0,
    "Noise Reduction": 10.0,
    "Optical Flow": 9.0,
    "Stabilization": 9.0,
    "Motion Blur": 8.0,
    "Composite": 8.0,
    "Time Remap": 7.0,
    "Chroma Key": 6.0,
    "Scaling": 5.0,
    "Blur": 4.0,
    "Sharpen": 3.0,
    "Grain": 3.0,
    "Generator": 2.0,
    "Color Correction": 2.0,
    "Transform": 1.5,
}

#: Host-specific names mapped onto the vocabulary above.  Matching is on
#: lowercased substrings, so "Gaussian Blur (Fast)" still resolves to "Blur".
EFFECT_ALIASES: dict[str, str] = {
    # composites / nested content
    "fusion": "Composite",
    "precomp": "Composite",
    "pre-comp": "Composite",
    "nested": "Composite",
    "compound": "Composite",
    "adjustment": "Composite",
    "multicam": "Composite",
    # noise reduction
    "temporal nr": "Temporal Noise Reduction",
    "temporal noise": "Temporal Noise Reduction",
    "spatial nr": "Noise Reduction",
    "denoise": "Noise Reduction",
    "neat video": "Noise Reduction",
    # retiming
    "optical flow": "Optical Flow",
    "frame blend": "Optical Flow",
    "twixtor": "Optical Flow",
    "time remap": "Time Remap",
    "timewarp": "Time Remap",
    "time warp": "Time Remap",
    "retime": "Time Remap",
    "speed": "Time Remap",
    # stabilisation
    "warp stabilizer": "Stabilization",
    "stabiliz": "Stabilization",
    "smoothcam": "Stabilization",
    # keying
    "ultra key": "Chroma Key",
    "keylight": "Chroma Key",
    "chroma key": "Chroma Key",
    "green screen": "Chroma Key",
    "luma key": "Chroma Key",
    # scaling
    "super scale": "Scaling",
    "upscale": "Scaling",
    "detail recovery": "Scaling",
    "resize": "Scaling",
    # colour
    "lumetri": "Color Correction",
    "color correct": "Color Correction",
    "colour correct": "Color Correction",
    "grade": "Color Correction",
    "lut": "Color Correction",
    # blur family
    "gaussian blur": "Blur",
    "lens blur": "Blur",
    "camera blur": "Blur",
    "directional blur": "Blur",
    "blur": "Blur",
    # misc
    "motion blur": "Motion Blur",
    "grain": "Grain",
    "sharpen": "Sharpen",
    "unsharp": "Sharpen",
    "transform": "Transform",
    "generator": "Generator",
    "solid": "Generator",
    "title": "Generator",
    "text": "Generator",
}

DEFAULT_EFFECT_COST = 3.0
BASE_CLIP_COST = 1.0


def normalize_effect_name(raw: str) -> str:
    """Map a host's effect name onto the neutral vocabulary.

    Unknown names are returned unchanged and priced at
    :data:`DEFAULT_EFFECT_COST`, so an unfamiliar effect still counts for
    something rather than silently costing nothing.
    """
    text = raw.strip()
    lowered = text.lower()
    if text in EFFECT_COST:
        return text
    for needle, canonical in EFFECT_ALIASES.items():
        if needle in lowered:
            return canonical
    for canonical in EFFECT_COST:
        if canonical.lower() in lowered:
            return canonical
    return text


class CostEstimator:
    """Turns a clip into a scalar render cost, learning as renders complete."""

    def __init__(
        self,
        table: Mapping[str, float] | None = None,
        default: float = DEFAULT_EFFECT_COST,
        base: float = BASE_CLIP_COST,
        measured: Mapping[str, float] | None = None,
    ) -> None:
        self.table = dict(table or EFFECT_COST)
        self.default = default
        self.base = base
        #: Segment name -> measured cost. Beats every estimate.
        self.measured: dict[str, float] = dict(measured or {})
        # effect name -> (sum_ratio, n) of measured/estimated ratios
        self._learned: dict[str, tuple[float, int]] = {}

    def effect_cost(self, effect: str) -> float:
        raw = self.table.get(effect)
        if raw is None:
            raw = self.table.get(normalize_effect_name(effect), self.default)
        ratio_sum, n = self._learned.get(effect, (0.0, 0))
        if n:
            raw *= ratio_sum / n
        return raw

    def estimate(self, clip: Clip) -> float:
        """Precedence: measured > explicit > effect table."""
        if clip.name in self.measured:
            return self.measured[clip.name]
        if clip.cost is not None:
            return float(clip.cost)
        return self.base + sum(self.effect_cost(e) for e in clip.effects)

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

    # ------------------------------------------------------------- profiles
    def load_profile(self, path: str | Path) -> int:
        """Load measured per-segment costs saved by a previous session."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.measured.update({k: float(v) for k, v in data.get("segments", {}).items()})
        return len(self.measured)

    def save_profile(self, path: str | Path, **meta) -> None:
        """Persist measured costs so the next session starts warm."""
        payload = {"segments": self.measured, **meta}
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
