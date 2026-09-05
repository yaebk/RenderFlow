"""Locate and import the DaVinci Resolve scripting module.

Resolve ships ``DaVinciResolveScript.py`` outside of ``sys.path``.  The standard
locations (per Blackmagic's ``README.txt`` in the Scripting folder) are:

Windows:
    %PROGRAMDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules
macOS:
    /Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules
Linux:
    /opt/resolve/Developer/Scripting/Modules  (or /home/resolve/...)

Resolve must be **running** with "External scripting using" set to Local (or
higher) in Preferences > System > General.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_module_dirs() -> list[Path]:
    env = os.environ
    if "RESOLVE_SCRIPT_API" in env:
        yield_dirs = [Path(env["RESOLVE_SCRIPT_API"]) / "Modules"]
    else:
        yield_dirs = []

    if sys.platform.startswith("win"):
        program_data = env.get("PROGRAMDATA", r"C:\ProgramData")
        yield_dirs.append(
            Path(program_data)
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
            / "Developer"
            / "Scripting"
            / "Modules"
        )
    elif sys.platform == "darwin":
        yield_dirs.append(
            Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve")
            / "Developer"
            / "Scripting"
            / "Modules"
        )
    else:
        yield_dirs += [
            Path("/opt/resolve/Developer/Scripting/Modules"),
            Path("/home/resolve/Developer/Scripting/Modules"),
        ]
    return [d for d in yield_dirs if d.is_dir()]


def _ensure_library_env() -> None:
    """Resolve's module also needs RESOLVE_SCRIPT_LIB pointing at the native lib."""
    if "RESOLVE_SCRIPT_LIB" in os.environ:
        return
    if sys.platform.startswith("win"):
        lib = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / (
            r"Blackmagic Design\DaVinci Resolve\fusionscript.dll"
        )
    elif sys.platform == "darwin":
        lib = Path(
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
        )
    else:
        lib = Path("/opt/resolve/libs/Fusion/fusionscript.so")
    if lib.exists():
        os.environ["RESOLVE_SCRIPT_LIB"] = str(lib)


def get_resolve():
    """Return a connected ``Resolve`` scripting object.

    Raises :class:`ImportError` if the module can't be found and ``RuntimeError``
    if Resolve is not running / scripting is disabled.
    """
    _ensure_library_env()

    try:
        import DaVinciResolveScript as dvr_script  # type: ignore
    except ImportError:
        for path in _candidate_module_dirs():
            sys.path.append(str(path))
        try:
            import DaVinciResolveScript as dvr_script  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ImportError(
                "DaVinciResolveScript not found. Install DaVinci Resolve and/or set "
                "RESOLVE_SCRIPT_API and RESOLVE_SCRIPT_LIB. Searched: "
                + ", ".join(str(p) for p in _candidate_module_dirs())
            ) from exc

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:  # pragma: no cover - depends on running app
        raise RuntimeError(
            "Connected to the Resolve scripting module but scriptapp('Resolve') "
            "returned None. Is DaVinci Resolve running with external scripting enabled "
            "(Preferences > System > General > External scripting using: Local)?"
        )
    return resolve
