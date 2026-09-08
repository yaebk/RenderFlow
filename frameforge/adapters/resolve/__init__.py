"""DaVinci Resolve adapter.

Resolve exposes no render-cache API, so this adapter does not try to drive
Resolve's cache. It builds one instead: each segment is rendered to a file via
the render queue and placed on a dedicated "FrameForge Cache" video track,
where it plays back with no effect processing.

    from frameforge import CacheEngine
    from frameforge.adapters.resolve import ResolveHost, get_resolve

    host = ResolveHost(get_resolve(), cache_dir="D:/ff_cache")
    engine = CacheEngine(host, host.read_timeline())
    engine.run(max_segments=10)
"""

from frameforge.adapters.resolve.connect import ResolveUnavailable, get_resolve
from frameforge.adapters.resolve.host import Baked, ResolveHost
from frameforge.adapters.resolve.reader import (
    CACHE_TRACK_NAME,
    detect_effects,
    frames_to_timecode,
    read_timeline,
    timecode_to_frames,
)

__all__ = [
    "ResolveHost",
    "Baked",
    "get_resolve",
    "ResolveUnavailable",
    "read_timeline",
    "detect_effects",
    "timecode_to_frames",
    "frames_to_timecode",
    "CACHE_TRACK_NAME",
]
