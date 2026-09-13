"""Wire format shared by the bridge server (inside Resolve) and the client.

Newline-delimited JSON over a localhost TCP socket, one request per line, one
response per line.

Resolve API objects cannot cross the wire, so the server keeps them in a
handle table and sends ``{"$handle": n, "$type": "..."}`` in their place. The
client hands the same marker back to call a method on that object or to pass
it as an argument (``timeline.DeleteClips([item])`` works over the bridge).
Handle 0 is always the ``resolve`` object itself.

Requests::

    {"id": 1, "token": "...", "op": "ping"}
    {"id": 2, "token": "...", "op": "call", "handle": 0, "method": "GetVersionString", "args": []}
    {"id": 3, "token": "...", "op": "release", "handle": 7}
    {"id": 4, "token": "...", "op": "clear"}
    {"id": 5, "token": "...", "op": "shutdown"}

Responses::

    {"id": 1, "ok": true,  "result": ...}
    {"id": 2, "ok": false, "error": {"type": "AttributeError", "message": "..."}}

This module runs on the Python that Resolve embeds: standard library only,
nothing newer than Python 3.6 syntax.
"""

import json
import os

PROTOCOL_VERSION = 1
ROOT_HANDLE = 0

HANDLE_KEY = "$handle"
TYPE_KEY = "$type"
ITEMS_KEY = "$items"      # dict whose keys are not all strings (e.g. GetMarkers)

DEFAULT_HOST = "127.0.0.1"


class BridgeError(Exception):
    """A request the bridge could not carry out (bad handle, bad op, auth)."""


def default_discovery_path():
    """Where a running bridge writes its port and token for clients to find."""
    return os.path.join(os.path.expanduser("~"), ".renderflow", "bridge.json")


class Handles:
    """Server-side table of live Resolve API objects, keyed by small ints."""

    def __init__(self, root):
        self._objects = {ROOT_HANDLE: root}
        self._next = ROOT_HANDLE + 1

    def register(self, obj):
        handle = self._next
        self._next += 1
        self._objects[handle] = obj
        return handle

    def get(self, handle):
        try:
            return self._objects[handle]
        except (KeyError, TypeError):
            raise BridgeError("unknown handle %r (released, or from an older bridge session)" % (handle,))

    def release(self, handle):
        if handle != ROOT_HANDLE:
            self._objects.pop(handle, None)

    def clear(self):
        root = self._objects[ROOT_HANDLE]
        self._objects = {ROOT_HANDLE: root}

    def __len__(self):
        return len(self._objects)


def is_primitive(value):
    return value is None or isinstance(value, (bool, int, float, str))


def encode(value, register):
    """Turn a Python value into something JSON can carry.

    ``register(obj) -> (handle, type_name)`` is called for anything that is
    not a primitive, list, or dict - on the server that stores the object in
    the handle table; on the client it only accepts remote proxies.
    """
    if is_primitive(value):
        return value
    if isinstance(value, (list, tuple)):
        return [encode(item, register) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: encode(item, register) for key, item in value.items()}
        return {ITEMS_KEY: [[encode(key, register), encode(item, register)]
                            for key, item in value.items()]}
    handle, type_name = register(value)
    return {HANDLE_KEY: handle, TYPE_KEY: type_name}


def decode(value, resolve_handle):
    """Inverse of :func:`encode`. ``resolve_handle(handle, type_name) -> obj``."""
    if isinstance(value, list):
        return [decode(item, resolve_handle) for item in value]
    if isinstance(value, dict):
        if HANDLE_KEY in value:
            return resolve_handle(value[HANDLE_KEY], value.get(TYPE_KEY))
        if ITEMS_KEY in value and len(value) == 1:
            return {decode(key, resolve_handle): decode(item, resolve_handle)
                    for key, item in value[ITEMS_KEY]}
        return {key: decode(item, resolve_handle) for key, item in value.items()}
    return value


def dumps(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def loads(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)
