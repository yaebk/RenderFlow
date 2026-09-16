"""Put the in-app launcher where Resolve's Workspace -> Scripts menu finds it.

``python -m renderflow install-bridge`` copies ``scripts/RenderFlow_Bridge.py``
into Resolve's Utility scripts folder with the path of this checkout filled
in, so the copy can import :mod:`renderflow` from Resolve's own Python.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SCRIPT_NAME = "RenderFlow_Bridge.py"
SOURCE = Path(__file__).resolve().parent.parent.parent / "scripts" / SCRIPT_NAME
REPO_LINE = re.compile(r'^REPO = r?".*".*$', re.MULTILINE)      # whole line, comment included


def scripts_dir(platform: str = sys.platform, environ: dict = os.environ) -> Path:
    """Resolve's per-user Utility scripts folder on this platform."""
    if platform == "win32":
        base = Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Scripts" / "Utility"
    if platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Blackmagic Design"
                / "DaVinci Resolve" / "Fusion" / "Scripts" / "Utility")
    return Path.home() / ".local" / "share" / "DaVinciResolve" / "Fusion" / "Scripts" / "Utility"


def install(dest_dir: Path | str | None = None, repo: Path | str | None = None,
            source: Path | str = SOURCE) -> Path:
    """Write the launcher into ``dest_dir`` with ``REPO`` set. Returns the copy's path."""
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"launcher not found at {source} - run this from a RenderFlow checkout")
    repo = Path(repo) if repo else source.parent.parent
    text = source.read_text("utf-8")
    line = f'REPO = r"{repo}"'
    text, n = REPO_LINE.subn(lambda _m: line, text, count=1)    # a callable: no backslash escapes
    if n != 1:
        raise RuntimeError(f"{source} has no REPO line to fill in")
    dest_dir = Path(dest_dir) if dest_dir else scripts_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / SCRIPT_NAME
    target.write_text(text, "utf-8")
    return target
