"""DaVinci Resolve integration layer for FrameForge (Phase 2+).

Nothing in :mod:`frameforge` imports this package - the scheduler stays
Resolve-agnostic.  Everything Resolve-specific lives here:

* :mod:`resolve.connect`  - bootstrap the Resolve scripting module.
* :mod:`resolve.adapter`  - read timeline/clips/playhead, drive the cache.
"""

from resolve.adapter import ResolveAdapter, ResolveUnavailable

__all__ = ["ResolveAdapter", "ResolveUnavailable"]
