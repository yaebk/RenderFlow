"""Get a handle on a running DaVinci Resolve.

Two ways in, and which one you get depends on your Resolve edition:

* **In-app** (works on free and Studio) - the script is run from
  Workspace -> Scripts or the Fusion Console, and Resolve injects a ``resolve``
  global. This is the only route on the free edition.
* **External** (Studio only) - a separate python.exe attaches via the
  ``DaVinciResolveScript`` module. Requires Preferences > System > General >
  "External scripting using" set to Local, a dropdown that only exists in
  Studio.

:func:`get_resolve` tries the injected global first, then the external route.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class ResolveUnavailable(RuntimeError):
    """Resolve could not be reached, with an explanation of why."""


def _module_dirs() -> list[Path]:
    dirs = []
    if "RESOLVE_SCRIPT_API" in os.environ:
        dirs.append(Path(os.environ["RESOLVE_SCRIPT_API"]) / "Modules")
    if sys.platform.startswith("win"):
        dirs.append(
            Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
            / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
        )
    elif sys.platform == "darwin":
        dirs.append(Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve"
                         "/Developer/Scripting/Modules"))
    else:
        dirs += [Path("/opt/resolve/Developer/Scripting/Modules"),
                 Path("/home/resolve/Developer/Scripting/Modules")]
    return [d for d in dirs if d.is_dir()]


def _ensure_lib_env() -> None:
    if "RESOLVE_SCRIPT_LIB" in os.environ:
        return
    if sys.platform.startswith("win"):
        lib = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / (
            "Blackmagic Design/DaVinci Resolve/fusionscript.dll")
    elif sys.platform == "darwin":
        lib = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents"
                   "/Libraries/Fusion/fusionscript.so")
    else:
        lib = Path("/opt/resolve/libs/Fusion/fusionscript.so")
    if lib.exists():
        os.environ["RESOLVE_SCRIPT_LIB"] = str(lib)


def get_resolve(injected=None):
    """Return a connected Resolve object.

    Pass the ``resolve`` global straight through when running in-app; it is
    used as-is and no import is attempted.
    """
    if injected is not None:
        return injected

    _ensure_lib_env()
    try:
        import DaVinciResolveScript as dvr
    except ImportError:
        for path in _module_dirs():
            sys.path.append(str(path))
        try:
            import DaVinciResolveScript as dvr
        except ImportError as exc:
            raise ResolveUnavailable(
                "DaVinciResolveScript not found. Searched: "
                + (", ".join(str(p) for p in _module_dirs()) or "(no known paths)")
            ) from exc

    handle = dvr.scriptapp("Resolve")
    if handle is None:
        raise ResolveUnavailable(
            "scriptapp('Resolve') returned None. Either Resolve is not running, or "
            "external scripting is unavailable - the free edition has no "
            "'External scripting using' preference, so run this from inside "
            "Resolve (Workspace -> Scripts) instead."
        )
    return handle
