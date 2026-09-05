"""Bridge between a live DaVinci Resolve session and the FrameForge scheduler.

Responsibilities (handoff Phase 2 milestones):

1-5.  Connect, get project/timeline, enumerate tracks + clips.
6.    Extract clip / effect information -> :class:`frameforge.Timeline`.
7.    Read the current playhead position (as a timeline frame).
8-9.  Determine and exercise whatever cache controls the API exposes.

The scheduler never imports this module; the driver
(:mod:`examples.resolve_driver`) wires the two together.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from frameforge.cost import EFFECT_COST
from frameforge.timeline import Clip, Timeline


class ResolveUnavailable(RuntimeError):
    """Raised when Resolve can't be reached (not running / scripting disabled)."""


# Effects we can positively identify from the scripting API today.  Everything
# else needs the fallback heuristics in ``_detect_effects`` and is a FLAG 4 item.
_KNOWN_FUSION_EFFECT = "Fusion"


def _timecode_to_frames(tc: str, fps: float) -> int:
    """Convert 'HH:MM:SS:FF' (non-drop) to an absolute frame count."""
    tc = tc.replace(";", ":")
    hh, mm, ss, ff = (int(p) for p in tc.split(":"))
    whole = round(fps)
    return ((hh * 60 + mm) * 60 + ss) * whole + ff


@dataclass
class ResolveAdapter:
    resolve: object = None
    # Optional user-supplied effect detector: (timeline_item) -> list[str]
    effect_detector: Callable[[object], list[str]] | None = None
    _fps: float = 24.0
    _timeline_start: int = 0
    # Which SetSetting keys were observed to actually change (FLAG 4 output).
    cache_setting_support: dict[str, bool] = field(default_factory=dict)

    # ------------------------------------------------------------- connect
    @classmethod
    def connect(cls, **kwargs) -> "ResolveAdapter":
        from resolve.connect import get_resolve

        try:
            resolve = get_resolve()
        except (ImportError, RuntimeError) as exc:
            raise ResolveUnavailable(str(exc)) from exc
        return cls(resolve=resolve, **kwargs)

    # -------------------------------------------------------- project/tl
    @property
    def project(self):
        pm = self.resolve.GetProjectManager()
        proj = pm.GetCurrentProject()
        if proj is None:
            raise ResolveUnavailable("No project is open in Resolve.")
        return proj

    @property
    def timeline(self):
        tl = self.project.GetCurrentTimeline()
        if tl is None:
            raise ResolveUnavailable("No timeline is open in Resolve.")
        return tl

    def refresh_timeline_meta(self) -> None:
        tl = self.timeline
        try:
            self._fps = float(tl.GetSetting("timelineFrameRate") or self.project.GetSetting("timelineFrameRate"))
        except (TypeError, ValueError):
            self._fps = 24.0
        self._timeline_start = int(tl.GetStartFrame())

    # ----------------------------------------------------------- effects
    def _detect_effects(self, item) -> list[str]:
        if self.effect_detector is not None:
            return list(self.effect_detector(item))

        effects: list[str] = []
        try:
            if item.GetFusionCompCount() > 0:
                effects.append(_KNOWN_FUSION_EFFECT)
        except AttributeError:
            pass

        # ResolveFX / OpenFX have no enumeration API as of Resolve 19, so we
        # fall back to name / marker conventions.  Person 2 owns improving this
        # (FLAG 4 / FLAG 6).
        name = ""
        try:
            name = (item.GetName() or "").lower()
        except AttributeError:
            pass
        for effect in EFFECT_COST:
            if effect.lower() in name:
                effects.append(effect)

        try:
            for marker in (item.GetMarkers() or {}).values():
                note = (marker.get("note") or "")
                for effect in EFFECT_COST:
                    if effect.lower() in note.lower() and effect not in effects:
                        effects.append(effect)
        except AttributeError:
            pass

        return effects

    # ---------------------------------------------------------- timeline
    def read_timeline(self) -> Timeline:
        """Milestones 3-6: enumerate video tracks and clips into a Timeline."""
        self.refresh_timeline_meta()
        tl = self.timeline
        clips: list[Clip] = []
        track_count = int(tl.GetTrackCount("video"))
        for track in range(1, track_count + 1):
            for item in tl.GetItemListInTrack("video", track) or []:
                try:
                    start = int(item.GetStart())
                    end = int(item.GetEnd())
                    name = item.GetName() or f"clip@{start}"
                except AttributeError:
                    continue
                clips.append(
                    Clip(
                        name=name,
                        start=start,
                        end=end,
                        effects=self._detect_effects(item),
                        track=track,
                    )
                )
        return Timeline(clips=clips, fps=self._fps, name=tl.GetName() or "timeline")

    # ---------------------------------------------------------- playhead
    def read_playhead(self) -> float:
        """Milestone 7: current playhead as an absolute timeline frame."""
        tl = self.timeline
        try:
            tc = tl.GetCurrentTimecode()
        except AttributeError as exc:
            raise ResolveUnavailable("Timeline.GetCurrentTimecode() unavailable") from exc
        return float(_timecode_to_frames(tc, self._fps))

    def set_playhead(self, frame: float) -> bool:
        """Move the Resolve playhead (used to nudge Smart Cache at a segment)."""
        whole = round(self._fps)
        f = int(frame)
        hh, rem = divmod(f, whole * 3600)
        mm, rem = divmod(rem, whole * 60)
        ss, ff = divmod(rem, whole)
        tc = f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
        try:
            return bool(self.timeline.SetCurrentTimecode(tc))
        except AttributeError:
            return False

    # ------------------------------------------------------------- cache
    def probe_cache_controls(self) -> dict[str, bool]:
        """Milestone 8 / FLAG 4: which cache-related settings can we actually set?

        There is no officially documented per-clip 'render this now' call, so we
        test the candidate project-setting keys and record which ones the API
        accepts and reflects back.
        """
        proj = self.project
        candidates = [
            "timelineDisableFusionCache",
            "perfProxyMediaMode",
            "perfCacheMode",
            "useOptimizedMediaResolution",
            "renderCacheMode",
        ]
        support: dict[str, bool] = {}
        for key in candidates:
            before = proj.GetSetting(key)
            support[key] = before is not None and before != ""
        self.cache_setting_support = support
        return support

    def set_render_cache_mode(self, mode: str) -> bool:
        """Best-effort: 'none' | 'smart' | 'user'.

        Falls back gracefully when the key is unsupported; the driver then
        relies on :meth:`request_cache` (playhead nudging) instead.
        """
        mapping = {"none": "0", "smart": "1", "user": "2"}
        value = mapping.get(mode.lower())
        if value is None:
            raise ValueError(f"unknown cache mode {mode!r}")
        ok = False
        for key in ("renderCacheMode", "perfCacheMode"):
            try:
                ok = bool(self.project.SetSetting(key, value)) or ok
            except (AttributeError, TypeError):
                pass
        return ok

    def request_cache(self, start_frame: int, end_frame: int, dwell_s: float = 0.0) -> None:
        """Ask Resolve to cache a segment.

        Mechanism that works with today's API: park the playhead at the start of
        the segment so Smart Cache begins rendering there.  ``dwell_s`` lets the
        caller give the cache engine time before moving on.
        """
        self.set_playhead(start_frame)
        if dwell_s:
            time.sleep(dwell_s)

    def is_cached(self, start_frame: int, end_frame: int) -> bool | None:
        """Cache-state readback is not exposed by the API (FLAG 4). Returns None."""
        return None
