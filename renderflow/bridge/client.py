"""The half of the bridge that runs outside Resolve.

    from renderflow import connect

    resolve = connect()                      # Studio: direct. Free: via the bridge.
    project = resolve.GetProjectManager().GetCurrentProject()
    print(project.GetName())

Over the bridge, ``resolve`` is a :class:`RemoteObject`: any attribute access
becomes a method call relayed to the real object inside Resolve. Return values
that are API objects come back as further ``RemoteObject`` proxies, and those
can be passed back in as arguments. Code written against the Resolve API
therefore runs unchanged on either edition.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, Callable

from renderflow.bridge import protocol
from renderflow.bridge.protocol import ROOT_HANDLE, default_discovery_path


class BridgeUnavailable(ConnectionError):
    """No bridge is running (or its discovery file is missing or stale)."""


class RemoteError(Exception):
    """The Resolve side raised. ``type`` is the original exception's class name."""

    def __init__(self, type_name: str, message: str):
        super().__init__(f"{type_name}: {message}")
        self.type = type_name
        self.message = message


class RemoteObject:
    """A Resolve API object living inside Resolve, addressed by handle."""

    __slots__ = ("_bridge", "_handle", "_type")

    def __init__(self, bridge: "Bridge", handle: int, type_name: str | None):
        self._bridge = bridge
        self._handle = handle
        self._type = type_name

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args: Any) -> Any:
            return self._bridge.call(self._handle, name, list(args))

        method.__name__ = name
        return method

    def __repr__(self) -> str:
        return f"<RemoteObject #{self._handle} {self._type or 'object'}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RemoteObject) and other._handle == self._handle

    def __hash__(self) -> int:
        return hash(self._handle)


class Bridge:
    """A connection to a :class:`~renderflow.bridge.server.BridgeServer`."""

    def __init__(self, host: str, port: int, token: str, timeout: float | None = None):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None
        self._next_id = 1

    # --------------------------------------------------------- discovery
    @classmethod
    def discover(cls, path: str | None = None, timeout: float | None = None) -> "Bridge":
        """Build a client from the discovery file a running bridge writes."""
        path = path or default_discovery_path()
        try:
            with open(path) as fh:
                info = json.load(fh)
            return cls(info["host"], int(info["port"]), info["token"], timeout=timeout)
        except FileNotFoundError:
            raise BridgeUnavailable(
                f"no bridge discovery file at {path} - start the bridge inside Resolve "
                "(Workspace -> Scripts -> RenderFlow_Bridge)"
            ) from None
        except (KeyError, ValueError, TypeError) as exc:
            raise BridgeUnavailable(f"discovery file {path} is unreadable: {exc}") from None

    # -------------------------------------------------------- connection
    def connect(self) -> "Bridge":
        if self._sock is not None:
            return self
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5.0)
        except OSError as exc:
            raise BridgeUnavailable(
                f"bridge at {self.host}:{self.port} is not answering ({exc}); "
                "is it still running inside Resolve?"
            ) from None
        sock.settimeout(self.timeout)
        self._sock = sock
        self._file = sock.makefile("rb")
        try:
            self.ping()
        except RemoteError:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "Bridge":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # --------------------------------------------------------- requests
    def request(self, op: str, **fields: Any) -> Any:
        if self._sock is None or self._file is None:
            raise BridgeUnavailable("not connected - call connect() first")
        request_id = self._next_id
        self._next_id += 1
        message = {"id": request_id, "token": self.token, "op": op}
        message.update(fields)
        try:
            self._sock.sendall(protocol.dumps(message))
            line = self._file.readline()
        except OSError as exc:
            self.close()
            raise BridgeUnavailable(f"bridge connection lost: {exc}") from None
        if not line:
            self.close()
            raise BridgeUnavailable("bridge closed the connection")
        response = protocol.loads(line)
        if response.get("id") != request_id:
            raise RemoteError("ProtocolError", f"response id {response.get('id')} != {request_id}")
        if not response.get("ok"):
            error = response.get("error") or {}
            raise RemoteError(error.get("type", "Error"), error.get("message", "unknown error"))
        return response.get("result")

    def ping(self) -> dict:
        return self.request("ping")

    def call(self, handle: int, method: str, args: list | None = None) -> Any:
        encoded = protocol.encode(args or [], self._encode_arg)
        result = self.request("call", handle=handle, method=method, args=encoded)
        return protocol.decode(result, self._wrap)

    def release(self, obj: RemoteObject) -> None:
        self.request("release", handle=obj._handle)

    def clear(self) -> None:
        """Drop every handle on the server except ``resolve`` itself."""
        self.request("clear")

    def shutdown(self) -> None:
        """Stop the bridge running inside Resolve."""
        try:
            self.request("shutdown")
        finally:
            self.close()

    @property
    def resolve(self) -> RemoteObject:
        return RemoteObject(self, ROOT_HANDLE, "Resolve")

    def _wrap(self, handle: int, type_name: str | None) -> RemoteObject:
        return RemoteObject(self, handle, type_name)

    @staticmethod
    def _encode_arg(value: Any) -> tuple[int, str | None]:
        if isinstance(value, RemoteObject):
            return value._handle, value._type
        raise TypeError(
            f"cannot send {type(value).__name__} over the bridge - only JSON values "
            "and RemoteObject proxies can be arguments"
        )


# ------------------------------------------------------------- connect()
_STUDIO_MODULE_DIRS = {
    "win32": [r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"],
    "darwin": ["/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"],
    "linux": ["/opt/resolve/Developer/Scripting/Modules",
              "/home/resolve/Developer/Scripting/Modules"],
}


def connect_direct() -> Any | None:
    """Attach through Blackmagic's own module. Works on Studio only; else None."""
    for directory in _STUDIO_MODULE_DIRS.get(sys.platform, []):
        directory = os.path.expandvars(directory)
        if os.path.isdir(directory) and directory not in sys.path:
            sys.path.append(directory)
    try:
        import DaVinciResolveScript as dvr  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return dvr.scriptapp("Resolve")
    except Exception:                                               # noqa: BLE001
        return None


def connect(prefer: str = "auto", timeout: float | None = None) -> Any:
    """Return a ``resolve`` object, whichever edition is installed.

    ``prefer`` is ``"auto"`` (direct if it works, else the bridge), ``"direct"``
    or ``"bridge"``. The result is either the genuine object from Blackmagic's
    module or a :class:`RemoteObject` - they are used the same way.
    """
    if prefer not in ("auto", "direct", "bridge"):
        raise ValueError(f"prefer must be auto, direct or bridge, not {prefer!r}")
    if prefer in ("auto", "direct"):
        resolve = connect_direct()
        if resolve is not None:
            return resolve
        if prefer == "direct":
            raise BridgeUnavailable(
                "direct connection failed - external scripting needs Resolve Studio; "
                "on the free edition use the bridge"
            )
    return Bridge.discover(timeout=timeout).connect().resolve
