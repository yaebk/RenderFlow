"""FrameForge - adaptive render-cache scheduler for DaVinci Resolve (single file).

Self-contained: no external packages, nothing to install, no sys.path edits.
Drop this one file into Resolve's script folder and run it from the menu.

============================================================================
INSTALL
============================================================================
Copy this file to:

    %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\frameforge.py

  i.e. on this machine:
    C:\\Users\\snake\\AppData\\Roaming\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\frameforge.py

Then in DaVinci Resolve:
    1. Open a project and a timeline (Edit page).
    2. Playback menu -> Render Cache -> Smart
    3. Workspace -> Scripts -> frameforge
       (restart Resolve once if it doesn't appear yet)

Alternatively: Workspace -> Console, set the dropdown to "Py3", and paste this
whole file in.

============================================================================
WHAT IT DOES
============================================================================
For RUN_SECONDS it polls the playhead a few times a second, recomputes a cache
priority for every timeline segment, and nudges Smart Cache toward the highest
priority one by parking the playhead there (Resolve exposes no direct per-clip
"render now" call). Priority combines:

    P(c) = w_cost * C   normalised render cost   (expensive work is worth caching)
         + w_prox * D   proximity to playhead    (near work is needed sooner)
         + w_dir  * V   playback-direction match (cache ahead of the cursor)
         + w_hist * H   revisit score            (sections you keep returning to)

Resolve's UI is sluggish while the loop runs (single-threaded script) - expected.
Set DRY_RUN = True to only print the schedule without moving the playhead.
"""

from __future__ import annotations

import heapq
import itertools
import time

# ==========================================================================
# CONFIG - edit these
# ==========================================================================
RUN_SECONDS = 60          # how long the loop runs before returning control
POLL_INTERVAL = 0.4       # seconds between playhead polls
SEGMENTS_PER_POLL = 1     # segments to (re)cache per poll
DRY_RUN = False           # True = print schedule only, never move the playhead

# Priority weights (see formula above).
W_COST = 1.0
W_PROX = 1.4
W_DIR = 0.8
W_HIST = 1.1
HORIZON_FRAMES = 480.0    # distance beyond which proximity/direction fade out


# ==========================================================================
# COST MODEL  (Phase 3 - static estimates; replace with measured timings later)
# ==========================================================================
EFFECT_COST = {
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
BASE_CLIP_COST = 1.0


class CostEstimator:
    def __init__(self):
        self.table = dict(EFFECT_COST)
        self._learned = {}  # effect -> (sum_ratio, n)

    def effect_cost(self, effect):
        raw = self.table.get(effect, DEFAULT_EFFECT_COST)
        ratio_sum, n = self._learned.get(effect, (0.0, 0))
        return raw * (ratio_sum / n) if n else raw

    def estimate(self, clip):
        return BASE_CLIP_COST + sum(self.effect_cost(e) for e in clip.effects)

    def observe(self, effects, measured_cost, estimated_cost):
        if estimated_cost <= 0 or not effects:
            return
        ratio = measured_cost / estimated_cost
        for effect in effects:
            s, n = self._learned.get(effect, (0.0, 0))
            self._learned[effect] = (s + ratio, n + 1)


# ==========================================================================
# TIMELINE DATA MODEL
# ==========================================================================
class Clip:
    __slots__ = ("name", "start", "end", "effects", "track")

    def __init__(self, name, start, end, effects=(), track=1):
        if end <= start:
            raise ValueError("clip %r: end <= start" % name)
        self.name = name
        self.start = int(start)
        self.end = int(end)
        self.effects = tuple(effects)
        self.track = int(track)

    @property
    def length(self):
        return self.end - self.start

    @property
    def midpoint(self):
        return (self.start + self.end) / 2.0

    def contains(self, frame):
        return self.start <= frame < self.end

    def distance_to(self, frame):
        if self.contains(frame):
            return 0.0
        if frame < self.start:
            return self.start - frame
        return frame - (self.end - 1)


class Timeline:
    def __init__(self, clips, fps=24.0, name="timeline"):
        self.clips = sorted(clips, key=lambda c: (c.track, c.start))
        self.fps = fps
        self.name = name

    def __iter__(self):
        return iter(self.clips)

    def __len__(self):
        return len(self.clips)

    @property
    def duration(self):
        return max((c.end for c in self.clips), default=0)

    def clip_at(self, frame, track=1):
        for clip in self.clips:
            if clip.track == track and clip.contains(frame):
                return clip
        return None


# ==========================================================================
# ADAPTIVE SCHEDULER  (Phases 1 & 4)
# ==========================================================================
class RenderJob:
    __slots__ = ("id", "name", "start", "end", "track", "est_cost",
                 "effects", "cached", "measured_cost", "priority")

    def __init__(self, id, name, start, end, track, est_cost, effects=()):
        self.id = id
        self.name = name
        self.start = start
        self.end = end
        self.track = track
        self.est_cost = est_cost
        self.effects = tuple(effects)
        self.cached = False
        self.measured_cost = None
        self.priority = 0.0

    @property
    def midpoint(self):
        return (self.start + self.end) / 2.0

    @property
    def length(self):
        return self.end - self.start

    def distance_to(self, frame):
        if self.start <= frame < self.end:
            return 0.0
        if frame < self.start:
            return self.start - frame
        return frame - (self.end - 1)


class Scheduler:
    def __init__(self, cost_estimator=None):
        self.cost = cost_estimator or CostEstimator()
        self._jobs = {}
        self._heap = []
        self._heap_dirty = True
        self._seq = itertools.count()

        self.playhead = 0.0
        self._playhead_history = []
        self.direction = 0
        self._visit_score = {}
        self._max_cost = 1.0

        # weights / tuning
        self.w_cost = W_COST
        self.w_prox = W_PROX
        self.w_dir = W_DIR
        self.w_hist = W_HIST
        self.horizon = HORIZON_FRAMES
        self.behind_weight = 0.25
        self.history_decay = 0.92
        self.direction_window = 6

    # ---- jobs ----
    def load_timeline(self, timeline, keep_cache=True):
        cached_ids = {j.id for j in self._jobs.values() if j.cached} if keep_cache else set()
        self._jobs.clear()
        for clip in timeline:
            jid = "%d:%s:%d" % (clip.track, clip.name, clip.start)
            job = RenderJob(jid, clip.name, clip.start, clip.end, clip.track,
                            self.cost.estimate(clip), clip.effects)
            job.cached = jid in cached_ids
            self._jobs[jid] = job
        self._max_cost = max((j.est_cost for j in self._jobs.values()), default=1.0)
        self._heap_dirty = True

    def pending_jobs(self):
        return [j for j in self._jobs.values() if not j.cached]

    def mark_cached(self, job_id, cached=True):
        job = self._jobs.get(job_id)
        if job and job.cached != cached:
            job.cached = cached
            self._heap_dirty = True

    def invalidate(self, job_id):
        self.mark_cached(job_id, False)

    # ---- playhead ----
    def update_playhead(self, position):
        prev = self.playhead
        self.playhead = float(position)

        hist = self._playhead_history
        hist.append(self.playhead)
        del hist[:-self.direction_window]
        if len(hist) >= 2:
            delta = hist[-1] - hist[0]
            self.direction = (delta > 0) - (delta < 0)

        for key in list(self._visit_score):
            self._visit_score[key] *= self.history_decay
            if self._visit_score[key] < 1e-3:
                del self._visit_score[key]

        if prev != self.playhead:
            for job in self._jobs.values():
                if job.distance_to(self.playhead) == 0.0:
                    self._visit_score[job.id] = self._visit_score.get(job.id, 0.0) + 1.0

        self._heap_dirty = True

    # ---- priority ----
    def calculate_priority(self, job):
        horizon = self.horizon
        c = job.est_cost / self._max_cost if self._max_cost else 0.0
        dist = job.distance_to(self.playhead)
        d = 1.0 / (1.0 + dist / horizon)

        if self.direction == 0:
            v = d
        else:
            ahead = (job.midpoint - self.playhead) * self.direction > 0
            reach = max(0.0, 1.0 - dist / (horizon * 1.5))
            v = reach if ahead else self.behind_weight * reach

        raw_hist = self._visit_score.get(job.id, 0.0)
        h = raw_hist / (1.0 + raw_hist)

        job.priority = (self.w_cost * c + self.w_prox * d
                        + self.w_dir * v + self.w_hist * h)
        return job.priority

    def _rebuild_heap(self):
        self._heap = []
        for job in self._jobs.values():
            if job.cached:
                continue
            heapq.heappush(self._heap,
                           (-self.calculate_priority(job), next(self._seq), job.id))
        self._heap_dirty = False

    def next_job(self):
        if self._heap_dirty:
            self._rebuild_heap()
        while self._heap:
            neg, seq, jid = heapq.heappop(self._heap)
            job = self._jobs.get(jid)
            if job and not job.cached:
                heapq.heappush(self._heap, (neg, seq, jid))
                return job
        return None

    def priority_order(self):
        return sorted(self.pending_jobs(), key=self.calculate_priority, reverse=True)

    # ---- learning ----
    def record_render_time(self, job, seconds):
        if isinstance(job, str):
            job = self._jobs[job]
        job.measured_cost = seconds
        self.cost.observe(job.effects, seconds, job.est_cost)
        job.est_cost = BASE_CLIP_COST + sum(self.cost.effect_cost(e) for e in job.effects)
        self._max_cost = max((j.est_cost for j in self._jobs.values()), default=1.0)
        self._heap_dirty = True


# ==========================================================================
# RESOLVE ADAPTER  (Phase 2 - read timeline/playhead, drive Smart Cache)
# ==========================================================================
def _timecode_to_frames(tc, fps):
    tc = tc.replace(";", ":")
    hh, mm, ss, ff = (int(p) for p in tc.split(":"))
    whole = int(round(fps))
    return ((hh * 60 + mm) * 60 + ss) * whole + ff


class ResolveAdapter:
    def __init__(self, resolve):
        self.resolve = resolve
        self._fps = 24.0

    @property
    def project(self):
        proj = self.resolve.GetProjectManager().GetCurrentProject()
        if proj is None:
            raise RuntimeError("No project is open in Resolve.")
        return proj

    @property
    def timeline(self):
        tl = self.project.GetCurrentTimeline()
        if tl is None:
            raise RuntimeError("No timeline is open in Resolve.")
        return tl

    def _refresh_meta(self):
        tl = self.timeline
        try:
            self._fps = float(tl.GetSetting("timelineFrameRate")
                              or self.project.GetSetting("timelineFrameRate"))
        except (TypeError, ValueError):
            self._fps = 24.0

    def _detect_effects(self, item):
        effects = []
        try:
            if item.GetFusionCompCount() > 0:
                effects.append("Fusion")
        except AttributeError:
            pass
        try:
            name = (item.GetName() or "").lower()
        except AttributeError:
            name = ""
        for effect in EFFECT_COST:
            if effect.lower() in name and effect not in effects:
                effects.append(effect)
        try:
            for marker in (item.GetMarkers() or {}).values():
                note = (marker.get("note") or "").lower()
                for effect in EFFECT_COST:
                    if effect.lower() in note and effect not in effects:
                        effects.append(effect)
        except AttributeError:
            pass
        return effects

    def read_timeline(self):
        self._refresh_meta()
        tl = self.timeline
        clips = []
        track_count = int(tl.GetTrackCount("video"))
        for track in range(1, track_count + 1):
            for item in tl.GetItemListInTrack("video", track) or []:
                try:
                    start = int(item.GetStart())
                    end = int(item.GetEnd())
                    name = item.GetName() or ("clip@%d" % start)
                except AttributeError:
                    continue
                clips.append(Clip(name, start, end, self._detect_effects(item), track))
        return Timeline(clips, fps=self._fps, name=tl.GetName() or "timeline")

    def read_playhead(self):
        return float(_timecode_to_frames(self.timeline.GetCurrentTimecode(), self._fps))

    def set_playhead(self, frame):
        whole = int(round(self._fps))
        f = int(frame)
        hh, rem = divmod(f, whole * 3600)
        mm, rem = divmod(rem, whole * 60)
        ss, ff = divmod(rem, whole)
        try:
            return bool(self.timeline.SetCurrentTimecode(
                "%02d:%02d:%02d:%02d" % (hh, mm, ss, ff)))
        except AttributeError:
            return False

    def request_cache(self, start_frame, end_frame, dwell_s=0.0):
        self.set_playhead(start_frame)
        if dwell_s:
            time.sleep(dwell_s)


# ==========================================================================
# MAIN LOOP
# ==========================================================================
def _get_resolve():
    try:
        return resolve  # injected global from the Scripts menu / Console
    except NameError:
        pass
    try:
        import DaVinciResolveScript as dvr_script
        r = dvr_script.scriptapp("Resolve")
    except ImportError:
        raise SystemExit(
            "Could not find the Resolve scripting module. Run this from inside "
            "DaVinci Resolve (Workspace -> Scripts, or the Py3 Console)."
        )
    if r is None:
        raise SystemExit(
            "scriptapp('Resolve') returned None - Resolve isn't running or "
            "external scripting is disabled."
        )
    return r


def main():
    adapter = ResolveAdapter(_get_resolve())

    timeline = adapter.read_timeline()
    est = CostEstimator()
    sched = Scheduler(cost_estimator=est)
    sched.load_timeline(timeline)

    print("FrameForge :: in-app adaptive cache")
    print("timeline %r  fps=%s  clips=%d" % (timeline.name, timeline.fps, len(timeline)))
    for c in timeline:
        print("  T%d %-20s [%6d-%-6d] cost~%5.1f  %s"
              % (c.track, c.name, c.start, c.end, est.estimate(c),
                 list(c.effects) or "(no effects detected)"))
    print("-" * 60)
    if DRY_RUN:
        print("DRY_RUN: playhead will NOT be moved.")

    deadline = time.time() + RUN_SECONDS
    dispatched = 0
    while time.time() < deadline:
        try:
            playhead = adapter.read_playhead()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print("playhead read failed: %s" % exc)
            time.sleep(POLL_INTERVAL)
            continue

        sched.update_playhead(playhead)
        order = sched.priority_order()
        if not order:
            print("all segments scheduled; stopping.")
            break

        for job in order[:SEGMENTS_PER_POLL]:
            print("playhead=%8.1f dir=%+d  ->  cache %-20s (priority %.3f, %d pending)"
                  % (playhead, sched.direction, job.name, job.priority,
                     len(sched.pending_jobs())))
            if not DRY_RUN:
                adapter.request_cache(job.start, job.end)
            sched.mark_cached(job.id)
            dispatched += 1

        time.sleep(POLL_INTERVAL)

    print("-" * 60)
    print("done. %d segments dispatched to cache." % dispatched)


main()
