"""Timeline ingest - read a timeline out of whatever your editor can export.

FrameForge does not talk to any editing application at runtime.  Instead it
reads the interchange formats every editor already writes, so one code path
covers all of them::

    from frameforge.formats import load
    timeline = load("my_edit.otio")

===============  =========================================================
``.json``        FrameForge's own format. No dependencies.
``.otio``        OpenTimelineIO native.
``.edl``         CMX 3600 - exported by essentially every NLE.
``.xml``         Final Cut Pro 7 XML - also written by Premiere and Resolve.
``.fcpxml``      Final Cut Pro X XML.
``.aaf``         Avid.
``.kdenlive``    Kdenlive.
===============  =========================================================

Everything except ``.json`` is read through OpenTimelineIO, which is an optional
dependency (``pip install "opentimelineio[view]"`` or ``otio-plugin-*``).  Call
:func:`supported_formats` to see what is actually available in this environment.
"""

from __future__ import annotations

from pathlib import Path

from frameforge.timeline import Timeline

OTIO_EXTENSIONS = {".otio", ".otioz", ".otiod", ".edl", ".xml", ".fcpxml", ".aaf", ".kdenlive"}
NATIVE_EXTENSIONS = {".json"}


def otio_available() -> bool:
    try:
        import opentimelineio  # noqa: F401
    except ImportError:
        return False
    return True


def supported_formats() -> dict[str, bool]:
    """Extension -> whether it can be read right now."""
    have_otio = otio_available()
    formats = {ext: True for ext in sorted(NATIVE_EXTENSIONS)}
    formats.update({ext: have_otio for ext in sorted(OTIO_EXTENSIONS)})
    return formats


def load(path: str | Path, **kwargs) -> Timeline:
    """Read a timeline from ``path``, dispatching on file extension."""
    path = Path(path)
    ext = path.suffix.lower()

    if ext in NATIVE_EXTENSIONS:
        from frameforge.formats.native import read_json

        return read_json(path, **kwargs)

    if ext in OTIO_EXTENSIONS:
        if not otio_available():
            raise ImportError(
                f"Reading {ext} needs OpenTimelineIO. Install it with:\n"
                '    pip install opentimelineio\n'
                "Or export your timeline as FrameForge JSON instead."
            )
        from frameforge.formats.otio import read_otio

        return read_otio(path, **kwargs)

    raise ValueError(
        f"Don't know how to read {ext!r}. Supported: "
        + ", ".join(sorted(NATIVE_EXTENSIONS | OTIO_EXTENSIONS))
    )


__all__ = ["load", "supported_formats", "otio_available"]
