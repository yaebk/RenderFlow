"""FrameForge's own timeline format - plain JSON, no dependencies.

Use this when your application already knows its own timeline and you just want
to hand it to FrameForge, or as an escape hatch when OpenTimelineIO isn't
available::

    {
      "fps": 24,
      "name": "my edit",
      "clips": [
        {"name": "A", "start": 0,   "end": 300, "effects": ["Blur"], "track": 1},
        {"name": "B", "start": 300, "end": 600, "cost": 9.0}
      ]
    }

``effects`` and ``cost`` are both optional; ``cost`` wins if you already know
what a clip costs to render.
"""

from __future__ import annotations

import json
from pathlib import Path

from frameforge.timeline import Clip, Timeline


def read_json(path: str | Path) -> Timeline:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return from_dict(data)


def from_dict(data: dict) -> Timeline:
    rows = data.get("clips", data.get("segments", []))
    clips = [
        Clip(
            name=str(row["name"]),
            start=int(row["start"]),
            end=int(row["end"]),
            effects=tuple(row.get("effects", ())),
            cost=row.get("cost"),
            track=int(row.get("track", 1)),
        )
        for row in rows
    ]
    return Timeline(
        clips=clips,
        fps=float(data.get("fps", 24.0)),
        name=str(data.get("name", "timeline")),
    )


def to_dict(timeline: Timeline) -> dict:
    return {
        "name": timeline.name,
        "fps": timeline.fps,
        "clips": [
            {
                "name": c.name,
                "start": c.start,
                "end": c.end,
                "effects": list(c.effects),
                **({"cost": c.cost} if c.cost is not None else {}),
                "track": c.track,
            }
            for c in timeline
        ],
    }


def write_json(timeline: Timeline, path: str | Path) -> None:
    Path(path).write_text(json.dumps(to_dict(timeline), indent=2), encoding="utf-8")
