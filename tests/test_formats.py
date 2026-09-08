"""Timeline ingest - native JSON, and the OTIO dispatch path."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frameforge.cost import normalize_effect_name
from frameforge.formats import load, otio_available, supported_formats
from frameforge.formats.native import from_dict, read_json, to_dict, write_json
from frameforge.timeline import Clip, Timeline

SAMPLE = {
    "name": "edit",
    "fps": 30,
    "clips": [
        {"name": "a", "start": 0, "end": 100, "effects": ["Blur"], "track": 1},
        {"name": "b", "start": 100, "end": 250, "cost": 7.5, "track": 2},
    ],
}


def test_from_dict_reads_all_fields():
    tl = from_dict(SAMPLE)
    assert tl.name == "edit" and tl.fps == 30.0 and len(tl) == 2
    a = next(c for c in tl if c.name == "a")
    b = next(c for c in tl if c.name == "b")
    assert a.effects == ("Blur",) and a.track == 1
    assert b.cost == 7.5 and b.track == 2


def test_optional_fields_default_sanely():
    tl = from_dict({"clips": [{"name": "x", "start": 0, "end": 10}]})
    clip = tl.clips[0]
    assert tl.fps == 24.0 and tl.name == "timeline"
    assert clip.effects == () and clip.cost is None and clip.track == 1


def test_json_round_trip(tmp_path):
    original = from_dict(SAMPLE)
    path = tmp_path / "edit.json"
    write_json(original, path)
    restored = read_json(path)
    assert to_dict(restored) == to_dict(original)


def test_load_dispatches_json(tmp_path):
    path = tmp_path / "edit.json"
    path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    assert len(load(path)) == 2


def test_load_rejects_unknown_extension(tmp_path):
    path = tmp_path / "edit.prproj"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Don't know how to read"):
        load(path)


def test_supported_formats_lists_native_as_always_available():
    formats = supported_formats()
    assert formats[".json"] is True
    assert formats[".otio"] == otio_available()
    assert ".edl" in formats and ".fcpxml" in formats and ".aaf" in formats


@pytest.mark.skipif(otio_available(), reason="OTIO is installed here")
def test_otio_extension_gives_an_actionable_error(tmp_path):
    path = tmp_path / "edit.otio"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ImportError, match="pip install opentimelineio"):
        load(path)


@pytest.mark.skipif(not otio_available(), reason="OTIO not installed")
def test_otio_round_trip(tmp_path):
    from frameforge.formats.otio import read_otio, write_otio

    original = Timeline(
        [Clip("a", 0, 100, track=1), Clip("b", 120, 250, track=1)], fps=24.0, name="t"
    )
    path = tmp_path / "edit.otio"
    write_otio(original, path)
    restored = read_otio(path)
    assert [(c.start, c.end) for c in restored] == [(0, 100), (120, 250)]


def _has_adapter(name: str) -> bool:
    if not otio_available():
        return False
    import opentimelineio as otio

    return any(a.name == name for a in otio.plugins.ActiveManifest().adapters)


EDL = """TITLE: FRAMEFORGE TEST
FCM: NON-DROP FRAME

001  AX       V     C        00:00:00:00 00:00:10:00 00:00:00:00 00:00:10:00
* FROM CLIP NAME: shot_a.mov
002  AX       V     C        00:00:00:00 00:00:05:00 00:00:10:00 00:00:15:00
* FROM CLIP NAME: shot_b.mov
003  AX       V     C        00:00:00:00 00:00:08:00 00:00:15:00 00:00:23:00
* FROM CLIP NAME: shot_c.mov
"""


@pytest.mark.skipif(not _has_adapter("cmx_3600"), reason="EDL adapter not installed")
def test_reads_a_real_cmx3600_edl(tmp_path):
    """The end-to-end universal-ingest claim: any NLE can export an EDL."""
    path = tmp_path / "edit.edl"
    path.write_text(EDL, encoding="utf-8")

    timeline = load(path)
    assert len(timeline) == 3
    assert [(c.start, c.end) for c in timeline] == [(0, 240), (240, 360), (360, 552)]
    assert all(c.track == 1 for c in timeline)


@pytest.mark.skipif(not _has_adapter("cmx_3600"), reason="EDL adapter not installed")
def test_edl_timeline_schedules(tmp_path):
    from frameforge import CacheEngine

    path = tmp_path / "edit.edl"
    path.write_text(EDL, encoding="utf-8")

    class Host:
        def __init__(self):
            self.done = []

        def render(self, segment):
            self.done.append(segment.name)

    host = Host()
    engine = CacheEngine(host, load(path))
    engine.set_playhead(260)
    engine.run(until_complete=True)
    assert host.done[0] == "shot_b.mov"      # the clip under the playhead
    assert len(host.done) == 3


# ------------------------------------------------------- effect vocabulary
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Fusion", "Composite"),
        ("Nested Sequence", "Composite"),
        ("Compound Clip", "Composite"),
        ("Adjustment Layer", "Composite"),
        ("Warp Stabilizer", "Stabilization"),
        ("Lumetri Color", "Color Correction"),
        ("Gaussian Blur", "Blur"),
        ("Ultra Key", "Chroma Key"),
        ("Neat Video", "Noise Reduction"),
        ("Temporal NR", "Temporal Noise Reduction"),
        ("Super Scale", "Scaling"),
        ("Optical Flow", "Optical Flow"),
        ("Motion Blur", "Motion Blur"),
    ],
)
def test_host_effect_names_normalise(raw, expected):
    assert normalize_effect_name(raw) == expected


def test_unknown_effect_survives_unchanged():
    assert normalize_effect_name("Weird Vendor Plugin") == "Weird Vendor Plugin"


def test_unknown_effect_still_costs_something():
    from frameforge.cost import DEFAULT_EFFECT_COST, CostEstimator

    est = CostEstimator()
    clip = Clip("x", 0, 10, effects=["Weird Vendor Plugin"])
    assert est.estimate(clip) == pytest.approx(est.base + DEFAULT_EFFECT_COST)
