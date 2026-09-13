"""Profile tests: a scripted fake ffmpeg for the logic, one real ffmpeg run
(skipped when ffmpeg is absent) for the integration."""

import os
import subprocess

import pytest

from renderflow.profile import (
    DecodeMeasure,
    FFmpegMissing,
    MeasurementCache,
    apply_measurements,
    decode_findings,
    find_ffmpeg,
    measure_decode,
    measured_text,
    parse_progress,
    profile,
    require_ffmpeg,
)
from renderflow.scan import Finding, ProjectSettings, ScanReport, TimelineInfo, clip_from_properties

PROGRESS = "frame=120\nfps=400.0\nframe=300\nprogress=end\n"


class FakeFFmpeg:
    """Answers like ffmpeg: a fixed frame count and a fixed wall time per call."""

    def __init__(self, frames=300, decode_wall=1.0, seek_wall=0.3, overhead=0.1):
        self.frames, self.decode_wall, self.seek_wall, self.overhead = frames, decode_wall, seek_wall, overhead
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[1] == "-version":
            return "ffmpeg version 9.0.1-test Copyright\n", "", 0.01
        if "lavfi" in cmd:
            return "", "", self.overhead
        if "-frames:v" in cmd:
            return "", "", self.seek_wall + self.overhead
        return f"frame={self.frames}\nprogress=end\n", "", self.decode_wall


def clip(**over):
    base = {"Clip Name": "cam.mp4", "File Path": r"D:\f\cam.mp4", "Type": "Video + Audio",
            "Video Codec": "H.264 High", "Resolution": "1920x1080", "FPS": 60.0,
            "Bit Depth": "8", "Frames": "3600", "Online Status": "Online", "Proxy": "None",
            "Usage": "1", "Super Scale": 1}
    base.update(over)
    return clip_from_properties(base, exists=lambda p: True, drive_type=lambda p: 3,
                                size_of=lambda p: 1)


def report(*clips):
    settings = ProjectSettings("2", "original", "none", "dnx", True, "dnx", 1)
    return ScanReport("p", "win32", TimelineInfo("t", 60.0, 1920, 1080, 1, len(clips)),
                      settings, list(clips))


# ------------------------------------------------------------------ units
def test_parse_progress_takes_last_frame_line():
    assert parse_progress(PROGRESS) == 300
    assert parse_progress("") == 0
    assert parse_progress("frame=abc\n") == 0


def test_require_ffmpeg_message_when_missing(monkeypatch):
    monkeypatch.setattr("renderflow.profile.find_ffmpeg", lambda: None)
    with pytest.raises(FFmpegMissing, match="winget"):
        require_ffmpeg()
    assert require_ffmpeg("/some/ffmpeg") == "/some/ffmpeg"


def test_find_ffmpeg_prefers_path(monkeypatch):
    monkeypatch.setattr("renderflow.profile.shutil.which", lambda name: "/usr/bin/ffmpeg")
    assert find_ffmpeg() == "/usr/bin/ffmpeg"


def test_measure_decode_computes_ratio_and_seek_minus_overhead():
    ff = FakeFFmpeg(frames=300, decode_wall=1.0, seek_wall=0.3, overhead=0.1)
    m = measure_decode(r"D:\f\cam.mp4", clip_fps=60.0, duration_s=60.0, sample_s=5.0,
                       seeks=4, exe="ffmpeg", runner=ff)
    assert m.frames == 300 and m.decode_fps == 300.0 and m.realtime_ratio == 5.0
    assert m.seek_ms == 300.0                 # 0.4 wall - 0.1 overhead, in ms
    assert m.verdict == "ok" and m.ffmpeg == "9.0.1-test"
    decode_cmd = ff.calls[0]
    assert "-ss" in decode_cmd and decode_cmd[decode_cmd.index("-ss") + 1] == "27.500"   # middle
    assert decode_cmd[decode_cmd.index("-t") + 1] == "5.000"
    assert sum(1 for c in ff.calls if "-frames:v" in c and "lavfi" not in c) == 4


def test_measure_decode_short_file_starts_at_zero_and_no_seeks():
    ff = FakeFFmpeg()
    m = measure_decode("x.mp4", 24.0, duration_s=2.0, sample_s=5.0, seeks=0, exe="ffmpeg", runner=ff)
    assert ff.calls[0][ff.calls[0].index("-ss") + 1] == "0.000"
    assert m.seek_ms is None


def test_measure_decode_with_hwaccel_flag():
    ff = FakeFFmpeg()
    m = measure_decode("x.mp4", 24.0, 10.0, hwaccel="d3d11va", exe="ffmpeg", runner=ff, seeks=1)
    assert m.hwaccel == "d3d11va"
    assert all("d3d11va" in c for c in ff.calls if "-i" in c and "lavfi" not in c)


def test_measure_decode_no_frames_is_an_error():
    ff = FakeFFmpeg(frames=0)
    with pytest.raises(RuntimeError, match="no frames"):
        measure_decode("bad.mp4", 24.0, 10.0, exe="ffmpeg", runner=ff, seeks=0)


def test_verdict_thresholds():
    def m(ratio):
        return DecodeMeasure("p", 5, 1, 1, ratio * 60, 60, ratio, None, None, "v", "cpu", 0)
    assert m(0.9).verdict == "below-realtime"
    assert m(1.5).verdict == "marginal"
    assert m(2.0).verdict == "ok"


def test_decode_findings_by_verdict_and_seek():
    c = clip()
    slow = DecodeMeasure(c.path, 5, 100, 5, 40.0, 60.0, 0.67, 400.0, None, "v", "cpu", 0)
    codes = [f.code for f in decode_findings(c, slow)]
    assert codes == ["decode-below-realtime", "seek-slow"]
    ok = DecodeMeasure(c.path, 5, 100, 5, 375.0, 60.0, 6.25, 150.0, None, "v", "cpu", 0)
    assert [f.code for f in decode_findings(c, ok)] == ["decode-ok"]
    marginal = DecodeMeasure(c.path, 5, 100, 5, 90.0, 60.0, 1.5, None, None, "v", "cpu", 0)
    assert [f.code for f in decode_findings(c, marginal)] == ["decode-marginal"]


# --------------------------------------------------------------- caching
def test_cache_round_trip(tmp_path):
    path = tmp_path / "m.json"
    m = DecodeMeasure("p", 5, 1, 1, 60, 60, 1, None, None, "v", "cpu", 0)
    MeasurementCache(path).put("k", m)
    assert MeasurementCache(path).get("k") == m
    assert MeasurementCache(path).get("other") is None
    assert MeasurementCache(path).get(None) is None


def test_cache_ignores_corrupt_file(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json")
    assert MeasurementCache(path).data == {}


# ---------------------------------------------------------------- profile
def test_profile_measures_each_path_once_and_rewrites_findings(tmp_path):
    a = clip()
    b = clip(**{"Clip Name": "same-file-again.mp4"})           # same path as a
    c = clip(**{"Clip Name": "missing.mp4", "File Path": r"D:\f\gone.mp4"})
    c.location = "missing"
    rep = report(a, b, c)
    rep.findings = [Finding("high", "codec-long-gop", "cam.mp4", "guess", "why"),
                    Finding("high", "media-missing", "missing.mp4", "gone", "why")]
    real_file = tmp_path / "cam.mp4"
    real_file.write_bytes(b"x")
    for cl in (a, b):
        cl.path = str(real_file)                                   # so the cache key can stat it

    ff = FakeFFmpeg(frames=300, decode_wall=1.0)
    cache = MeasurementCache(tmp_path / "cache.json")
    log = []
    results = profile(rep, sample_s=5.0, seeks=0, exe="ffmpeg", cache=cache, runner=ff,
                      progress=log.append)

    assert list(results) == [str(real_file)]                      # a and b share it; c skipped
    assert sum(1 for cmd in ff.calls if "-progress" in cmd) == 1  # measured once
    codes = [f.code for f in rep.findings]
    assert "codec-long-gop" not in codes                          # guess replaced
    assert codes.count("decode-ok") == 2 and "media-missing" in codes
    assert a.measured["decode_fps"] == 300.0 and b.measured["realtime_ratio"] == 5.0
    assert c.measured is None
    assert any("measuring" in line for line in log)

    # Second run hits the cache: no decode command at all.
    ff2 = FakeFFmpeg()
    profile(report(clip()), exe="ffmpeg", cache=cache, runner=ff2)
    ff3 = FakeFFmpeg()
    a2 = clip()
    a2.path = str(real_file)
    profile(report(a2), sample_s=5.0, seeks=0, exe="ffmpeg", cache=cache, runner=ff3)
    assert not any("-progress" in cmd for cmd in ff3.calls)


def test_profile_skips_clip_ffmpeg_cannot_read(tmp_path):
    bad = clip()
    f = tmp_path / "bad.mp4"
    f.write_bytes(b"x")
    bad.path = str(f)
    rep = report(bad)
    log = []
    results = profile(rep, exe="ffmpeg", cache=MeasurementCache(None), runner=FakeFFmpeg(frames=0),
                      seeks=0, progress=log.append)
    assert results == {} and bad.measured is None
    assert any("skipped" in line for line in log)


def test_measured_text_and_report_json_include_measurements():
    c = clip()
    rep = report(c)
    m = DecodeMeasure(c.path, 5, 300, 1, 300.0, 60.0, 5.0, 120.0, None, "9.0", "cpu-x", 0)
    apply_measurements(rep, {c.path: m})
    text = measured_text(rep, {c.path: m})
    assert "cam.mp4" in text and "5x" in text and "120 ms" in text and "cpu-x" in text
    assert rep.to_dict()["clips"][0]["measured"]["decode_fps"] == 300.0
    assert measured_text(rep, {}) == "no clips measured."


# ------------------------------------------------------------ real ffmpeg
@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg not installed")
def test_real_ffmpeg_on_a_generated_clip(tmp_path):
    exe = find_ffmpeg()
    sample = tmp_path / "test.mp4"
    subprocess.run([exe, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=30:duration=3", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(sample)], check=True)
    assert sample.exists()
    m = measure_decode(str(sample), clip_fps=30.0, duration_s=3.0, sample_s=1.0, seeks=2, exe=exe)
    assert 25 <= m.frames <= 31
    assert m.decode_fps > 30 and m.realtime_ratio > 1
    assert m.seek_ms is not None and m.seek_ms >= 0
    assert os.path.basename(m.path) == "test.mp4"
