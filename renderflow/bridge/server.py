"""The half of the bridge that runs inside DaVinci Resolve.

:class:`BridgeServer` owns the ``resolve`` object, a handle table, and a
localhost TCP listener. Every request is checked against a per-session token,
decoded, run against the API under a lock (one API call at a time, whatever
the client does), and the result encoded back.

The request handling is separate from the socket code so it can be tested
against a fake ``resolve`` with no network at all - see ``handle_request``.

Runs on the Python Resolve embeds: standard library only, Python 3.6 syntax.
"""

import hmac
import json
import os
import secrets
import socketserver
import threading
import time

from renderflow.bridge import protocol
from renderflow.bridge.protocol import (
    DEFAULT_HOST,
    ROOT_HANDLE,
    BridgeError,
    Handles,
    default_discovery_path,
)


def _type_name(obj):
    return type(obj).__name__


class BridgeServer(object):
    """Relay JSON requests to the Resolve API.

    ``resolve`` is the object Resolve hands to in-app scripts. ``port=0`` picks
    a free port; the client finds it through the discovery file.
    """

    def __init__(self, resolve, host=DEFAULT_HOST, port=0, token=None,
                 discovery_path=None, on_shutdown=None):
        self.resolve = resolve
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.discovery_path = discovery_path or default_discovery_path()
        self.on_shutdown = on_shutdown
        self.handles = Handles(resolve)
        self.started_at = None
        self.requests_served = 0
        self._lock = threading.Lock()
        self._tcp = None
        self._thread = None

    # ------------------------------------------------------------ requests
    def handle_request(self, request):
        """Carry out one decoded request and return the response dict."""
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise BridgeError("request must be a JSON object")
            self._check_token(request.get("token"))
            op = request.get("op")
            if op == "ping":
                result = self._ping()
            elif op == "call":
                result = self._call(request)
            elif op == "release":
                self.handles.release(request.get("handle"))
                result = True
            elif op == "clear":
                self.handles.clear()
                result = True
            elif op == "shutdown":
                threading.Thread(target=self.stop, daemon=True).start()
                result = True
            else:
                raise BridgeError("unknown op %r" % (op,))
            self.requests_served += 1
            return {"id": request_id, "ok": True, "result": result}
        except Exception as exc:                                    # noqa: BLE001
            return {
                "id": request_id,
                "ok": False,
                "error": {"type": _type_name(exc), "message": str(exc)},
            }

    def _check_token(self, token):
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
            raise BridgeError("bad or missing token")

    def _ping(self):
        version = None
        try:
            version = self.resolve.GetVersionString()
        except Exception:                                           # noqa: BLE001
            pass
        return {
            "protocol": protocol.PROTOCOL_VERSION,
            "resolve": version,
            "handles": len(self.handles),
            "requests": self.requests_served,
            "uptime": (time.time() - self.started_at) if self.started_at else 0.0,
        }

    def _call(self, request):
        handle = request.get("handle", ROOT_HANDLE)
        method = request.get("method")
        if not isinstance(method, str) or not method.isidentifier() or method.startswith("_"):
            raise BridgeError("method must be a public identifier, got %r" % (method,))
        target = self.handles.get(handle)
        args = protocol.decode(request.get("args") or [], lambda h, _t: self.handles.get(h))
        with self._lock:
            attr = getattr(target, method)
            result = attr(*args) if callable(attr) else attr
        return protocol.encode(result, self._register)

    def _register(self, obj):
        return self.handles.register(obj), _type_name(obj)

    # -------------------------------------------------------------- sockets
    @property
    def address(self):
        if self._tcp is None:
            return (self.host, self.port)
        return self._tcp.server_address[:2]

    def start(self):
        """Listen in a background thread and write the discovery file."""
        if self._tcp is not None:
            return self.address
        self._tcp = _Listener((self.host, self.port), self)
        self.host, self.port = self._tcp.server_address[:2]
        self.started_at = time.time()
        self._write_discovery()
        self._thread = threading.Thread(
            target=self._tcp.serve_forever, args=(0.05,), name="renderflow-bridge", daemon=True
        )
        self._thread.start()
        return self.address

    def wait(self, poll=0.5):
        """Block the calling thread until :meth:`stop` is called."""
        while self._tcp is not None:
            time.sleep(poll)

    def stop(self):
        tcp, self._tcp = self._tcp, None
        if tcp is not None:
            tcp.shutdown()
            tcp.server_close()
        self._remove_discovery()
        if self.on_shutdown is not None:
            try:
                self.on_shutdown()
            except Exception:                                       # noqa: BLE001
                pass

    @property
    def running(self):
        return self._tcp is not None

    # ------------------------------------------------------------ discovery
    def _write_discovery(self):
        directory = os.path.dirname(self.discovery_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        info = {
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "started": self.started_at,
            "protocol": protocol.PROTOCOL_VERSION,
        }
        with open(self.discovery_path, "w") as fh:
            json.dump(info, fh)

    def _remove_discovery(self):
        try:
            with open(self.discovery_path) as fh:
                if json.load(fh).get("token") != self.token:
                    return          # a newer bridge owns the file now
            os.remove(self.discovery_path)
        except (OSError, ValueError):
            pass


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        while True:
            line = self.rfile.readline()
            if not line:
                return
            if not line.strip():
                continue
            try:
                request = protocol.loads(line)
            except ValueError as exc:
                response = {"id": None, "ok": False,
                            "error": {"type": "ValueError", "message": "bad JSON: %s" % exc}}
            else:
                response = bridge.handle_request(request)
            try:
                self.wfile.write(protocol.dumps(response))
            except (TypeError, ValueError) as exc:
                self.wfile.write(protocol.dumps({
                    "id": response.get("id"), "ok": False,
                    "error": {"type": "TypeError", "message": "result not serialisable: %s" % exc},
                }))


class _Listener(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, bridge):
        self.bridge = bridge
        socketserver.ThreadingTCPServer.__init__(self, address, _Handler)
