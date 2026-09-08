"""OpenTimelineIO ingest - one reader for every editor's export format.

OTIO is the industry interchange format for editorial timelines, with adapters
for CMX 3600 EDL, Final Cut Pro 7 XML, FCP X XML, AAF (Avid) and Kdenlive.
Premiere, Resolve, Media Composer and Final Cut can all write at least one of
those, so this single path covers them without any of them cooperating at
runtime.

EFFECT FIDELITY
---------------
Interchange formats carry effect *names* inconsistently and effect *parameters*
almost never.  Whatever survives the round trip is read into ``Clip.effects``
and priced from the static table in :mod:`frameforge.cost`, which makes it a
starting guess only.  Once :class:`~frameforge.engine.CacheEngine` starts
rendering, measured times replace those guesses - that is the whole reason the
cost model does not depend on effect detection.
"""

from __future__ import annotations

from pathlib import Path

from frameforge.cost import normalize_effect_name
from frameforge.timeline import Clip, Timeline


def _iter_clips(track):
    """OTIO renamed ``each_clip`` to ``find_clips``; support both."""
    finder = getattr(track, "find_clips", None) or getattr(track, "each_clip", None)
    if finder is None:  # pragma: no cover - very old OTIO
        return []
    return finder()


def _clip_effects(clip) -> tuple[str, ...]:
    names: list[str] = []
    for effect in getattr(clip, "effects", None) or []:
        raw = getattr(effect, "effect_name", None) or getattr(effect, "name", None)
        if raw:
            names.append(normalize_effect_name(str(raw)))
    # A time-warp on a clip means optical flow / frame interpolation work.
    if type(getattr(clip, "media_reference", None)).__name__ == "GeneratorReference":
        names.append("Generator")
    return tuple(names)


def read_otio(path: str | Path, include_audio: bool = False) -> Timeline:
    import opentimelineio as otio

    tl = otio.adapters.read_from_file(str(path))

    try:
        fps = float(tl.duration().rate)
    except (AttributeError, ValueError):
        fps = 24.0

    tracks = list(tl.video_tracks())
    if include_audio:
        tracks += list(tl.audio_tracks())

    clips: list[Clip] = []
    for index, track in enumerate(tracks, start=1):
        for otio_clip in _iter_clips(track):
            try:
                span = otio_clip.range_in_parent()
            except Exception:  # noqa: BLE001 - gaps and odd items are skippable
                continue
            start = int(round(span.start_time.value_rescaled_to(fps)))
            length = int(round(span.duration.value_rescaled_to(fps)))
            if length <= 0:
                continue
            clips.append(
                Clip(
                    name=otio_clip.name or f"clip@{start}",
                    start=start,
                    end=start + length,
                    effects=_clip_effects(otio_clip),
                    track=index,
                )
            )

    return Timeline(clips=clips, fps=fps, name=tl.name or Path(path).stem)


def write_otio(timeline: Timeline, path: str | Path) -> None:
    """Write a FrameForge timeline back out as OTIO (one track per input track)."""
    import opentimelineio as otio

    out = otio.schema.Timeline(name=timeline.name)
    by_track: dict[int, list[Clip]] = {}
    for clip in timeline:
        by_track.setdefault(clip.track, []).append(clip)

    for track_index in sorted(by_track):
        track = otio.schema.Track(name=f"V{track_index}")
        cursor = 0
        for clip in sorted(by_track[track_index], key=lambda c: c.start):
            if clip.start > cursor:
                track.append(
                    otio.schema.Gap(
                        source_range=otio.opentime.TimeRange(
                            otio.opentime.RationalTime(0, timeline.fps),
                            otio.opentime.RationalTime(clip.start - cursor, timeline.fps),
                        )
                    )
                )
            track.append(
                otio.schema.Clip(
                    name=clip.name,
                    source_range=otio.opentime.TimeRange(
                        otio.opentime.RationalTime(0, timeline.fps),
                        otio.opentime.RationalTime(clip.length, timeline.fps),
                    ),
                )
            )
            cursor = clip.end
        out.tracks.append(track)

    otio.adapters.write_to_file(out, str(path))
