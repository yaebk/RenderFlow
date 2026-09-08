"""Read a Resolve timeline into FrameForge's neutral data model.

Effect detection here is better than what interchange formats give us, because
the colour page node graph is fully enumerable:

    graph.GetNumNodes()          -> how deep the grade is
    graph.GetToolsInNode(i)      -> the actual tools in each node

That is real per-clip effect information, which no EDL or FCP XML carries. It
still only seeds the cold-start estimate - measured render times replace it as
soon as the engine starts working.
"""

from __future__ import annotations

from frameforge.cost import normalize_effect_name
from frameforge.timeline import Clip, Timeline

#: Track holding FrameForge's baked segments. Excluded from scheduling so the
#: cache never tries to cache itself.
CACHE_TRACK_NAME = "FrameForge Cache"


def timecode_to_frames(tc: str, fps: float) -> int:
    """'HH:MM:SS:FF' (or drop-frame 'HH:MM:SS;FF') to an absolute frame count."""
    hh, mm, ss, ff = (int(p) for p in tc.replace(";", ":").split(":"))
    whole = int(round(fps))
    return ((hh * 60 + mm) * 60 + ss) * whole + ff


def frames_to_timecode(frame: float, fps: float) -> str:
    whole = int(round(fps))
    hh, rem = divmod(int(frame), whole * 3600)
    mm, rem = divmod(rem, whole * 60)
    ss, ff = divmod(rem, whole)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def timeline_fps(timeline, project=None) -> float:
    for source in (timeline, project):
        if source is None:
            continue
        try:
            value = source.GetSetting("timelineFrameRate")
        except (AttributeError, TypeError):
            continue
        try:
            if value:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 24.0


def detect_effects(item) -> list[str]:
    """Everything we can learn about what makes this clip expensive."""
    effects: list[str] = []

    try:
        if item.GetFusionCompCount() > 0:
            effects.append("Composite")
    except AttributeError:
        pass

    # Colour page grade: every tool in every node.
    try:
        graph = item.GetNodeGraph()
        node_count = int(graph.GetNumNodes()) if graph else 0
    except (AttributeError, TypeError, ValueError):
        graph, node_count = None, 0

    for index in range(1, node_count + 1):
        try:
            tools = graph.GetToolsInNode(index) or []
        except (AttributeError, TypeError):
            continue
        for tool in tools:
            effects.append(normalize_effect_name(str(tool)))

    # A grade with many nodes costs something even when the tools are unnamed.
    if node_count > 1 and not effects:
        effects.extend(["Color Correction"] * (node_count - 1))

    return effects


def cache_track_index(timeline) -> int | None:
    """Index of the FrameForge cache track, or None if it doesn't exist yet."""
    try:
        count = int(timeline.GetTrackCount("video"))
    except (AttributeError, TypeError, ValueError):
        return None
    for index in range(1, count + 1):
        try:
            if timeline.GetTrackName("video", index) == CACHE_TRACK_NAME:
                return index
        except (AttributeError, TypeError):
            continue
    return None


def read_timeline(timeline, project=None, skip_cache_track: bool = True) -> Timeline:
    """Enumerate video tracks and clips into a FrameForge Timeline."""
    fps = timeline_fps(timeline, project)
    skip = cache_track_index(timeline) if skip_cache_track else None

    clips: list[Clip] = []
    for track in range(1, int(timeline.GetTrackCount("video")) + 1):
        if track == skip:
            continue  # never schedule our own baked output
        for item in timeline.GetItemListInTrack("video", track) or []:
            try:
                start, end = int(item.GetStart()), int(item.GetEnd())
                name = item.GetName() or f"clip@{start}"
            except (AttributeError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            clips.append(Clip(name, start, end, detect_effects(item), track=track))

    return Timeline(clips, fps=fps, name=timeline.GetName() or "timeline")
