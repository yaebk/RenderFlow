"""The half of the bridge that runs inside DaVinci Resolve.

:class:`BridgeServer` owns the ``resolve`` object, a handle table, and a
localhost TCP listener. Every request is checked against a per-session token,
decoded, run against the API, and the result encoded back.

The server has no threads of its own. Inside Resolve the script's main thread
sits in the UI toolkit's event loop, which never yields to other Python
threads, so a server thread would never run. Instead everything happens in
:meth:`poll`, which the caller drives from its own loop: the in-app launcher
alternates UI event pumping with ``poll()``. Hosts that can run a thread (the
tests) use :meth:`start`.

Request handling (:meth:`handle_request`) is separate from the socket code so
it can be tested against a fake ``resolve`` with no network.

Runs on the Python Resolve embeds: standard library only, Python 3.6 syntax.
"""

import hmac
import json
import os
import secrets
import select
import socket
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

SEND_TIMEOUT_S = 10.0


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
        self._listener = None
        self._clients = {}          # socket -> unread bytes
        self._stop_requested = False
        self._thread = None
        self._close_lock = threading.Lock()

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
                self._stop_requested = True     # the reply still goes out first
                result = True
            else:
                raise BridgeError("unknown op %r" % (op,))
            self.requests_served += 1
            return {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
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
        except Exception:
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
        attr = getattr(target, method)
        result = attr(*args) if callable(attr) else attr
        return protocol.encode(result, self._register)

    def _register(self, obj):
        return self.handles.register(obj), _type_name(obj)

    # -------------------------------------------------------------- sockets
    @property
    def address(self):
        return (self.host, self.port)

    @property
    def running(self):
        return self._listener is not None

    def listen(self):
        """Open the port and write the discovery file. Then drive :meth:`poll`."""
        if self._listener is not None:
            return self.address
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(8)
        listener.setblocking(False)
        self.host, self.port = listener.getsockname()[:2]
        self._listener = listener
        self._stop_requested = False
        self.started_at = time.time()
        self._write_discovery()
        return self.address

    def poll(self, timeout=0.05):
        """Service the socket for up to ``timeout`` seconds. Returns False once stopped."""
        if self._listener is None:
            return False
        if self._stop_requested:
            self.stop()
            return False
        watch = [self._listener] + list(self._clients)
        try:
            readable, _, _ = select.select(watch, [], [], timeout)
        except (OSError, ValueError):
            return self.running
        for sock in readable:
            if sock is self._listener:
                self._accept()
            else:
                self._read(sock)
        return self.running

    def serve_forever(self, timeout=0.05):
        """Run :meth:`poll` on the calling thread until stopped."""
        self.listen()
        while self.poll(timeout):
            pass

    def start(self):
        """Listen and run :meth:`serve_forever` in a thread (for hosts that can)."""
        address = self.listen()
        self._thread = threading.Thread(
            target=self.serve_forever, name="renderflow-bridge", daemon=True
        )
        self._thread.start()
        return address

    def stop(self):
        """Close everything, remove the discovery file, notify ``on_shutdown``."""
        self._stop_requested = True
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._close_lock:
            listener, self._listener = self._listener, None
            if listener is None:
                return
            for sock in list(self._clients):
                self._drop(sock)
            listener.close()
            self._remove_discovery()
        if self.on_shutdown is not None:
            try:
                self.on_shutdown()
            except Exception:
                pass

    def _accept(self):
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        conn.settimeout(SEND_TIMEOUT_S)
        self._clients[conn] = b""

    def _drop(self, sock):
        self._clients.pop(sock, None)
        try:
            sock.close()
        except OSError:
            pass

    def _read(self, sock):
        try:
            data = sock.recv(65536)
        except OSError:
            data = b""
        if not data:
            self._drop(sock)
            return
        buffer = self._clients.get(sock, b"") + data
        while b"\n" in buffer and sock in self._clients:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                self._respond(sock, line)
        if sock in self._clients:
            self._clients[sock] = buffer

    def _respond(self, sock, line):
        try:
            request = protocol.loads(line)
        except ValueError as exc:
            response = {"id": None, "ok": False,
                        "error": {"type": "ValueError", "message": "bad JSON: %s" % exc}}
        else:
            response = self.handle_request(request)
        try:
            payload = protocol.dumps(response)
        except (TypeError, ValueError) as exc:
            payload = protocol.dumps({
                "id": response.get("id"), "ok": False,
                "error": {"type": "TypeError", "message": "result not serialisable: %s" % exc},
            })
        try:
            sock.sendall(payload)
        except OSError:
            self._drop(sock)

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
