"""Fusion tool attribution: costs come from bypassing tools one at a time, and
nothing is left bypassed afterwards - not even when the run is interrupted."""

import json

import pytest
from test_rendercost import FakeItem, FakeProject, FakeResolve, FakeTimeline

from renderflow import tools as tools_mod
from renderflow.rendercost import RenderQueue, render_cost
from renderflow.tools import (
    Bypassed,
    CompCost,
    ToolCost,
    ToolReport,
    attribute,
    candidates,
    restore_bypassed,
    tool_findings,
)


@pytest.fixture(autouse=True)
def _files_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("renderflow.rendercost.RENDER_CACHE_PATH", tmp_path / "render.json")
    monkeypatch.setattr(tools_mod, "BYPASSED_PATH", tmp_path / "bypassed.json")


# ------------------------------------------------------------------ fakes
class CostlyTool:
    """A Fusion tool with a per-frame cost that disappears when bypassed."""

    def __init__(self, name, reg_id, ms):
        self.attrs = {"TOOLS_Name": name, "TOOLS_RegID": reg_id, "TOOLB_PassThrough": False}
        self.ms = ms
        self.sets = []

    def GetAttrs(self):
        return dict(self.attrs)

    def SetAttrs(self, attrs):
        self.sets.append(dict(attrs))
        self.attrs.update(attrs)
        return True


class CostlyComp:
    def __init__(self, *tools):
        self.tools = {i: t for i, t in enumerate(tools, 1)}

    def GetToolList(self):
        return self.tools


class CostlyItem(FakeItem):
    """Per-frame cost = base + every active tool."""

    def __init__(self, name, start, end, base, comps=()):
        super().__init__(name, start, end, base, comps)
        self.base = base

    @property
    def ms_per_frame(self):
        return self.base + sum(t.ms for c in self.comps for t in c.tools.values()
                               if not t.attrs.get("TOOLB_PassThrough"))

    @ms_per_frame.setter
    def ms_per_frame(self, value):
        self.base = value


def heavy_comp():
    return CostlyComp(CostlyTool("MediaIn1", "MediaIn", 0), CostlyTool("Grain1", "Grain", 100.0),
                      CostlyTool("Grain1Red", "LUTBezier", 0), CostlyTool("CC1", "ColorCorrector", 5.0),
                      CostlyTool("Noise", "FastNoise", 60.0), CostlyTool("MediaOut1", "MediaOut", 0))


def make(items):
    project = FakeProject(FakeTimeline([items]))
    return FakeResolve(project), project


def all_active(*comps):
    return all(not t.attrs["TOOLB_PassThrough"] for c in comps for t in c.tools.values())


# ---------------------------------------------------------------- measure
def test_attribute_measures_each_tool_and_puts_everything_back(tmp_path):
    a, b = heavy_comp(), heavy_comp()
    items = [CostlyItem("fx", 0, 6000, 5.0, [a]), CostlyItem("copy", 6000, 12000, 5.0, [b]),
             CostlyItem("plain", 12000, 18000, 5.0)]
    resolve, project = make(items)
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "r")))
    assert [round(rc.ratio(s), 2) for s in rc.samples] == [0.1, 0.1, 3.33]
    log = []
    tr = attribute(resolve, rc, progress=log.append, queue=RenderQueue(resolve, target_dir=str(tmp_path / "t")))
    assert tr.problems == []
    assert [c.label for c in tr.comps] == ["fx @V1 00:00:00:00"]          # the identical comp measured once
    (c,) = tr.comps
    assert c.copies == [{"label": "copy @V1 00:01:40:00", "track": 1, "start": 6000}]
    assert c.frames == 90 and c.ok
    assert round(c.ms_per_frame) == 170                                    # 5 + 100 + 5 + 60, set-up removed
    assert {(t.name, t.kind, round(t.saved_ms_per_frame)) for t in c.tools} == {
        ("Grain1", "Grain", 100), ("CC1", "ColorCorrector", 5), ("Noise", "FastNoise", 60)}
    # every tool is back exactly as it was, the on-disk record is gone, the copy was never touched
    assert all_active(a, b)
    grain = a.tools[2]
    assert grain.sets == [{"TOOLB_PassThrough": True}, {"TOOLB_PassThrough": False}]
    assert all(t.sets == [] for t in b.tools.values())
    assert not (tmp_path / "bypassed.json").exists()
    assert log[0].startswith("comp 1/1: fx @V1 00:00:00:00 - 3 tool(s), 90 frames each")
    text = tr.text()
    assert "Grain1  Grain               100 ms/frame" in text and "+1 more copies" in text
    assert "do not add up" in text

    found = tool_findings(tr)
    assert [f.code for f in found] == ["fusion-tool-heavy"] and found[0].severity == "high"
    assert found[0].message.startswith("170 ms/frame; the cost is mostly Grain1 (Grain, ~100 ms/frame), "
                                       "Noise (FastNoise, ~60 ms/frame)")
    assert "CC1" not in found[0].message                                  # 5 ms: under half a frame at 60 fps
    assert "on 1 more clip(s)" in found[0].why


def test_a_tool_the_comp_cannot_render_without_is_reported_not_dropped(tmp_path):
    comp = heavy_comp()
    resolve, project = make([CostlyItem("fx", 0, 6000, 5.0, [comp])])
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "r")))
    queue = RenderQueue(resolve, target_dir=str(tmp_path / "t"))
    orig = queue.render_range

    def render_range(a, b, name="renderflow_sample"):
        if comp.tools[4].attrs["TOOLB_PassThrough"]:           # CC1 bypassed -> Resolve refuses
            return 0.0, "Failed"
        return orig(a, b, name)

    queue.render_range = render_range
    tr = attribute(resolve, rc, queue=queue)
    (c,) = tr.comps
    assert [(t.name, t.ok) for t in c.tools] == [("Grain1", True), ("CC1", False), ("Noise", True)]
    assert "CC1     ColorCorrector        - the comp does not render with it bypassed (Failed)" in tr.text()
    assert all_active(comp) and tr.problems == []
    assert "CC1" not in tool_findings(tr)[0].message


def test_no_finding_for_a_comp_that_renders_in_real_time():
    fast = CompCost("x", 1, 0, 600, 1, 90, 7.0, [ToolCost("Flicker", "ofx.Flicker", 30.0)])
    assert tool_findings(ToolReport("t", 30.0, [fast])) == []


def test_attribute_skips_tools_the_editor_bypassed_and_short_clips(tmp_path):
    comp = heavy_comp()
    comp.tools[2].attrs["TOOLB_PassThrough"] = True                        # editor turned Grain off
    short = CostlyComp(CostlyTool("Blur1", "Blur", 40.0))
    items = [CostlyItem("fx", 0, 6000, 5.0, [comp]), CostlyItem("blip", 6000, 6010, 5.0, [short])]
    resolve, project = make(items)
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "r")))
    tr = attribute(resolve, rc, queue=RenderQueue(resolve, target_dir=str(tmp_path / "t")))
    (c,) = tr.comps                                                        # blip: not heavy on its own, no stretch
    assert [t.name for t in c.tools] == ["CC1", "Noise"]                   # Grain left alone, not measured
    assert comp.tools[2].sets == [] and comp.tools[2].attrs["TOOLB_PassThrough"] is True


def test_candidates_include_fusion_clips_inside_a_heavy_stretch(tmp_path):
    cuts = [CostlyItem(f"c{k}", k * 50, (k + 1) * 50, 5.0, [heavy_comp()] if k % 2 == 0 else [])
            for k in range(6)]
    resolve, project = make(cuts)
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "r")))
    assert [s.label for s in candidates(rc)] == ["c0 @V1 00:00:00:00", "c2 @V1 00:00:01:40", "c4 @V1 00:00:03:20"]
    tr = attribute(resolve, rc, queue=RenderQueue(resolve, target_dir=str(tmp_path / "t")))
    (c,) = tr.comps
    assert c.frames == 50 and len(c.copies) == 2 and all_active(*(i.comps[0] for i in cuts if i.comps))


def test_interrupted_run_is_repaired_by_the_next_one(tmp_path):
    comp = heavy_comp()
    items = [CostlyItem("fx", 0, 6000, 5.0, [comp])]
    resolve, project = make(items)
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "r")))

    # a render that blows up mid-attribution: the tool is put back by the finally
    boom = RenderQueue(resolve, target_dir=str(tmp_path / "t"))
    calls = []

    def render_range(a, b, name="renderflow_sample", _orig=boom.render_range):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Resolve went away")
        return _orig(a, b, name)

    boom.render_range = render_range
    with pytest.raises(RuntimeError, match="went away"):
        attribute(resolve, rc, queue=boom)
    assert all_active(comp) and not (tmp_path / "bypassed.json").exists()

    # the process died with the record on disk: the next run re-enables the tool first
    grain = comp.tools[2]
    grain.attrs["TOOLB_PassThrough"] = True
    Bypassed(tmp_path / "bypassed.json").add({"timeline": "Timeline 1", "label": "fx @V1 00:00:00:00",
                                              "track": 1, "start": 0, "comp": 1, "tool": "Grain1"})
    log = []
    tr = attribute(resolve, rc, progress=log.append, queue=RenderQueue(resolve, target_dir=str(tmp_path / "u")))
    assert log[0] == "re-enabled 1 tool(s) an interrupted run left bypassed: Grain1 on fx @V1 00:00:00:00"
    assert all_active(comp) and not (tmp_path / "bypassed.json").exists() and tr.problems == []

    # a record for a tool that no longer exists is kept, not silently dropped
    rec = Bypassed(tmp_path / "bypassed.json")
    rec.add({"timeline": "Timeline 1", "label": "x", "track": 1, "start": 0, "comp": 1, "tool": "Gone"})
    assert restore_bypassed(project, rec) == [] and len(Bypassed(tmp_path / "bypassed.json").entries) == 1


def test_a_tool_that_will_not_come_back_is_reported_loudly(tmp_path):
    comp = heavy_comp()
    stuck = comp.tools[2]
    real_set = stuck.SetAttrs

    def sticky(attrs):                                 # accepts the bypass, refuses the restore
        if attrs.get("TOOLB_PassThrough") is False:
            return False
        return real_set(attrs)

    stuck.SetAttrs = sticky
    resolve, project = make([CostlyItem("fx", 0, 6000, 5.0, [comp])])
    rc = render_cost(resolve, queue=RenderQueue(resolve, target_dir=str(tmp_path / "r")))
    tr = attribute(resolve, rc, queue=RenderQueue(resolve, target_dir=str(tmp_path / "t")))
    assert tr.problems == ["Grain1 (Grain) on fx @V1 00:00:00:00 may still be bypassed"]
    assert "PROBLEMS - check these tools in Fusion" in tr.text()
    entries = json.loads((tmp_path / "bypassed.json").read_text())["entries"]
    assert [e["tool"] for e in entries] == ["Grain1"]                      # the record stays for undo


def test_tool_report_text_and_findings_without_comps():
    tr = ToolReport("t", 30.0, [])
    assert tr.text().startswith("no Fusion comps to attribute")
    c = CompCost("x", 1, 0, 100, 1, 50, 20.0, [ToolCost("a", "Blur", 4.0)])
    assert tool_findings(ToolReport("t", 30.0, [c])) == []                 # 4 ms: nothing stands out
