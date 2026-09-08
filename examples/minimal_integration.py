"""The smallest useful FrameForge integration - start here.

A host is any object with a ``render(segment)`` method.  That's the whole
required contract.  Everything below the ``MyRenderer`` class is FrameForge.

    python examples/minimal_integration.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge import CacheEngine, Timeline, describe_host
from frameforge.formats.native import from_dict


# ---------------------------------------------------------------------------
# 1. Your renderer. FrameForge never looks inside this.
# ---------------------------------------------------------------------------
class MyRenderer:
    """Stands in for a real frame renderer (FFmpeg, OpenGL, a DCC, anything)."""

    def __init__(self) -> None:
        self.cache: dict[str, list[int]] = {}

    def render(self, segment) -> None:
        # Pretend expensive segments really are expensive.
        time.sleep(0.002 * segment.cost)
        self.cache[segment.name] = list(segment.frames())

    # --- optional: implement any of these and FrameForge will use them ---

    def is_cached(self, segment) -> bool:
        return segment.name in self.cache

    def evict(self, segment) -> None:
        self.cache.pop(segment.name, None)


# ---------------------------------------------------------------------------
# 2. Your timeline. Load it from a file with frameforge.formats.load(), or
#    build it inline like this if your app already knows its own edit.
# ---------------------------------------------------------------------------
TIMELINE = {
    "name": "demo",
    "fps": 24,
    "clips": [
        {"name": "intro", "start": 0, "end": 240, "effects": ["Color Correction"]},
        {"name": "action", "start": 240, "end": 600, "effects": ["Motion Blur"]},
        {"name": "plain", "start": 600, "end": 840, "effects": []},
        {"name": "hero", "start": 840, "end": 1200, "effects": ["Noise Reduction", "Composite"]},
        {"name": "outro", "start": 1200, "end": 1440, "effects": ["Blur"]},
    ],
}


def main() -> None:
    host = MyRenderer()
    timeline: Timeline = from_dict(TIMELINE)

    print(describe_host(host), "\n")

    engine = CacheEngine(host, timeline)
    print(f"{len(timeline)} clips -> {engine.pending} segments to cache\n")

    # Pretend the user parks the playhead in the middle of the expensive shot.
    engine.set_playhead(900)

    print("cache order from frame 900:")
    while engine.pending:
        segment = engine.next_segment()
        results = engine.step()
        if not results:
            break
        r = results[0]
        print(f"  {r.segment.name:<12} cost~{r.segment.cost:5.1f}  "
              f"rendered in {r.seconds * 1000:6.1f} ms")

    print("\nstats:", engine.stats())

    # Measured costs now replace the static guesses.
    print("\nmeasured costs (seconds):")
    for name, seconds in sorted(engine.cost.measured.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<12} {seconds * 1000:6.1f} ms")


if __name__ == "__main__":
    main()
