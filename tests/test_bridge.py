"""Bridge tests: protocol round-trips, request handling against a fake Resolve,
and a full client<->server exchange over a real localhost socket."""

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from renderflow.bridge import protocol
from renderflow.bridge.client import (
    Bridge,
    BridgeUnavailable,
    RemoteError,
    RemoteObject,
    connect,
)
from renderflow.bridge.protocol import ROOT_HANDLE, BridgeError, Handles
from renderflow.bridge.server import BridgeServer


# ------------------------------------------------------------------ fakes
class FakeItem:
    def __init__(self, name, start, end):
        self.name, self.start, self.end = name, start, end
        self.deleted = False

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetClipProperty(self, key=None):
        props = {"Video Codec": "H.265", "Resolution": "3840x2160"}
        return props if key is None else props.get(key)


class FakeTimeline:
    def __init__(self):
        self.name = "Timeline 1"
        self.items = [FakeItem("a", 0, 48), FakeItem("b", 48, 120)]
        self.markers = {12: {"color": "Blue", "name": "m"}}

    def GetName(self):
        return self.name

    def GetTrackCount(self, kind):
        return 1 if kind == "video" else 0

    def GetItemListInTrack(self, kind, index):
        return list(self.items) if (kind, index) == ("video", 1) else []

    def GetMarkers(self):
        return dict(self.markers)

    def DeleteClips(self, items, ripple=False):
        for item in items:
            item.deleted = True
        return True

    def SetName(self, name):
        self.name = name
        return True


class FakeProject:
    def __init__(self):
        self.timeline = FakeTimeline()

    def GetName(self):
        return "Demo"

    def GetCurrentTimeline(self):
        return self.timeline

    def Explode(self):
        raise ValueError("boom")


class FakeProjectManager:
    def __init__(self):
        self.project = FakeProject()

    def GetCurrentProject(self):
        return self.project


class FakeResolve:
    def __init__(self):
        self.pm = FakeProjectManager()
        self.page = "edit"

    def GetVersionString(self):
        return "19.0"

    def GetProjectManager(self):
        return self.pm

    def GetCurrentPage(self):
        return self.page


# -------------------------------------------------------------- fixtures
@pytest.fixture
def fake():
    return FakeResolve()


@pytest.fixture
def discovery(tmp_path):
    return str(tmp_path / "bridge.json")


@pytest.fixture
def server(fake, discovery):
    srv = BridgeServer(fake, port=0, discovery_path=discovery)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server, discovery):
    with Bridge.discover(discovery, timeout=5.0) as bridge:
        yield bridge


def req(server, op, **fields):
    return server.handle_request(dict(token=server.token, op=op, id=1, **fields))


# -------------------------------------------------------------- protocol
def test_encode_primitives_and_containers_pass_through():
    reg = lambda obj: (99, "X")
    value = {"a": [1, 2.5, "s", None, True], "b": {"c": (1, 2)}}
    assert protocol.encode(value, reg) == {"a": [1, 2.5, "s", None, True], "b": {"c": [1, 2]}}


def test_encode_objects_become_handles_and_decode_back():
    handles = Handles(root="ROOT")
    obj = object()
    encoded = protocol.encode([obj, {"k": obj}], lambda o: (handles.register(o), "T"))
    assert encoded[0] == {"$handle": 1, "$type": "T"}
    decoded = protocol.decode(encoded, lambda h, t: handles.get(h))
    assert decoded[0] is obj and decoded[1]["k"] is obj


def test_int_keyed_dicts_survive_the_wire():
    """GetMarkers() returns {frame: {...}} - JSON would turn the keys into strings."""
    markers = {12: {"name": "m"}, 40: {"name": "n"}}
    encoded = protocol.encode(markers, None)
    assert "$items" in encoded
    line = protocol.dumps(encoded)
    assert protocol.decode(protocol.loads(line), None) == markers


def test_handles_root_survives_clear_and_release():
    handles = Handles(root="ROOT")
    h = handles.register("x")
    handles.release(ROOT_HANDLE)
    handles.clear()
    assert handles.get(ROOT_HANDLE) == "ROOT"
    with pytest.raises(BridgeError):
        handles.get(h)


# ------------------------------------------------- server without sockets
def test_call_on_root(fake, discovery):
    srv = BridgeServer(fake, discovery_path=discovery)
    resp = req(srv, "call", handle=0, method="GetVersionString", args=[])
    assert resp == {"id": 1, "ok": True, "result": "19.0"}


def test_call_chain_returns_handles(fake, discovery):
    srv = BridgeServer(fake, discovery_path=discovery)
    pm = req(srv, "call", handle=0, method="GetProjectManager", args=[])["result"]
    assert pm["$handle"] == 1 and pm["$type"] == "FakeProjectManager"
    project = req(srv, "call", handle=1, method="GetCurrentProject", args=[])["result"]
    name = req(srv, "call", handle=project["$handle"], method="GetName", args=[])
    assert name["result"] == "Demo"


def test_bad_token_rejected(fake, discovery):
    srv = BridgeServer(fake, discovery_path=discovery)
    resp = srv.handle_request({"id": 5, "token": "nope", "op": "ping"})
    assert resp["ok"] is False and "token" in resp["error"]["message"]
    resp = srv.handle_request({"id": 6, "op": "ping"})
    assert resp["ok"] is False


def test_unknown_op_handle_and_private_method_are_errors(fake, discovery):
    srv = BridgeServer(fake, discovery_path=discovery)
    assert req(srv, "dance")["ok"] is False
    assert req(srv, "call", handle=42, method="GetName", args=[])["ok"] is False
    assert req(srv, "call", handle=0, method="__class__", args=[])["ok"] is False
    assert req(srv, "call", handle=0, method="_pm", args=[])["ok"] is False


def test_api_exception_is_reported_not_raised(fake, discovery):
    srv = BridgeServer(fake, discovery_path=discovery)
    project = req(srv, "call", handle=0, method="GetProjectManager", args=[])["result"]
    project = req(srv, "call", handle=project["$handle"], method="GetCurrentProject", args=[])
    resp = req(srv, "call", handle=project["result"]["$handle"], method="Explode", args=[])
    assert resp["ok"] is False
    assert resp["error"] == {"type": "ValueError", "message": "boom"}


def test_non_json_request_line_gets_an_error_response(server):
    host, port = server.address
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(b"this is not json\n")
        line = sock.makefile("rb").readline()
    resp = json.loads(line)
    assert resp["ok"] is False and "bad JSON" in resp["error"]["message"]


# --------------------------------------------------------------- sockets
def test_discovery_file_written_and_removed(fake, discovery):
    srv = BridgeServer(fake, port=0, discovery_path=discovery)
    host, port = srv.start()
    with open(discovery) as fh:
        info = json.load(fh)
    assert info["port"] == port and info["token"] == srv.token and host == "127.0.0.1"
    srv.stop()
    assert not os.path.exists(discovery)


def test_stop_leaves_a_newer_bridges_discovery_file_alone(fake, discovery):
    old = BridgeServer(fake, port=0, discovery_path=discovery)
    old.start()
    new = BridgeServer(fake, port=0, discovery_path=discovery)
    new.start()
    old.stop()
    assert os.path.exists(discovery)
    with open(discovery) as fh:
        assert json.load(fh)["token"] == new.token
    new.stop()


def test_client_walks_the_api_like_the_real_thing(client):
    resolve = client.resolve
    assert resolve.GetVersionString() == "19.0"
    project = resolve.GetProjectManager().GetCurrentProject()
    assert isinstance(project, RemoteObject)
    assert project.GetName() == "Demo"
    timeline = project.GetCurrentTimeline()
    items = timeline.GetItemListInTrack("video", 1)
    assert [item.GetName() for item in items] == ["a", "b"]
    assert items[1].GetClipProperty("Video Codec") == "H.265"
    assert items[0].GetClipProperty() == {"Video Codec": "H.265", "Resolution": "3840x2160"}


def test_client_can_pass_remote_objects_back_as_arguments(client, fake):
    timeline = client.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    items = timeline.GetItemListInTrack("video", 1)
    assert timeline.DeleteClips([items[0]], False) is True
    assert fake.pm.project.timeline.items[0].deleted is True
    assert fake.pm.project.timeline.items[1].deleted is False


def test_client_sees_int_keyed_markers(client):
    timeline = client.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    assert timeline.GetMarkers() == {12: {"color": "Blue", "name": "m"}}


def test_client_side_effects_reach_resolve(client, fake):
    timeline = client.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    assert timeline.SetName("Renamed") is True
    assert fake.pm.project.timeline.name == "Renamed"


def test_remote_exception_surfaces_as_remote_error(client):
    project = client.resolve.GetProjectManager().GetCurrentProject()
    with pytest.raises(RemoteError) as info:
        project.Explode()
    assert info.value.type == "ValueError" and info.value.message == "boom"


def test_unserialisable_argument_is_a_local_type_error(client):
    with pytest.raises(TypeError):
        client.resolve.GetProjectManager().GetCurrentProject().SetName(object())


def test_release_and_clear(client, server):
    project = client.resolve.GetProjectManager().GetCurrentProject()
    assert len(server.handles) == 3
    client.release(project)
    assert len(server.handles) == 2
    with pytest.raises(RemoteError):
        project.GetName()
    client.clear()
    assert len(server.handles) == 1
    assert client.resolve.GetVersionString() == "19.0"     # root always survives


def test_calls_are_serialised_across_connections(server, discovery):
    """Two clients hammering the bridge at once never corrupt each other's replies."""
    errors = []

    def worker():
        try:
            with Bridge.discover(discovery, timeout=5.0) as bridge:
                for _ in range(50):
                    assert bridge.resolve.GetVersionString() == "19.0"
                    assert bridge.resolve.GetCurrentPage() == "edit"
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_ping_reports_status(client):
    info = client.ping()
    assert info["resolve"] == "19.0" and info["protocol"] == protocol.PROTOCOL_VERSION
    assert info["requests"] >= 1


def test_shutdown_from_client_stops_server(fake, discovery):
    stopped = threading.Event()
    srv = BridgeServer(fake, port=0, discovery_path=discovery, on_shutdown=stopped.set)
    srv.start()
    bridge = Bridge.discover(discovery).connect()
    bridge.shutdown()
    assert stopped.wait(5.0)
    assert not srv.running and not os.path.exists(discovery)
    assert not bridge.connected


# --------------------------------------------------------------- connect()
def test_discover_without_file_is_bridge_unavailable(tmp_path):
    with pytest.raises(BridgeUnavailable):
        Bridge.discover(str(tmp_path / "missing.json"))


def test_stale_discovery_file_is_bridge_unavailable(tmp_path):
    path = tmp_path / "bridge.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 1, "token": "t"}))
    with pytest.raises(BridgeUnavailable):
        Bridge.discover(str(path)).connect()


def test_connect_falls_back_to_bridge_when_direct_fails(server, discovery, monkeypatch):
    monkeypatch.setattr("renderflow.bridge.client.connect_direct", lambda: None)
    monkeypatch.setattr("renderflow.bridge.protocol.default_discovery_path", lambda: discovery)
    monkeypatch.setattr("renderflow.bridge.client.default_discovery_path", lambda: discovery)
    resolve = connect()
    assert isinstance(resolve, RemoteObject)
    assert resolve.GetVersionString() == "19.0"
    resolve._bridge.close()


def test_connect_prefers_direct_when_available(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("renderflow.bridge.client.connect_direct", lambda: sentinel)
    assert connect() is sentinel
    assert connect(prefer="direct") is sentinel


class LikeBlackmagics:
    """Blackmagic's PyRemoteObject: every attribute exists and unknown ones are None."""

    def __getattr__(self, name):
        return None if name.startswith("_") else (lambda *a: "direct")

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return None


def test_bridge_cli_recognises_a_direct_connection(monkeypatch, capsys):
    from renderflow.bridge.__main__ import main
    monkeypatch.setattr("renderflow.bridge.__main__.connect", lambda prefer: LikeBlackmagics())
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "connected directly (Studio)" in out and "project  : none open" in out


def test_connect_direct_only_raises_on_free_edition(monkeypatch):
    monkeypatch.setattr("renderflow.bridge.client.connect_direct", lambda: None)
    with pytest.raises(BridgeUnavailable):
        connect(prefer="direct")


# ------------------------------------------------- the in-Resolve situation
def test_poll_driven_server_needs_no_threads_of_its_own(fake, discovery):
    """How the in-app launcher runs it: the script's own loop calls poll()."""
    srv = BridgeServer(fake, port=0, discovery_path=discovery)
    srv.listen()
    results = []

    def client_side():
        with Bridge.discover(discovery, timeout=5.0) as bridge:
            results.append(bridge.resolve.GetProjectManager().GetCurrentProject().GetName())
            bridge.shutdown()

    t = threading.Thread(target=client_side)
    t.start()
    steps = 0
    while srv.poll(0.05):            # stand-in for: disp.StepLoop(); server.poll()
        steps += 1
        assert steps < 2000, "server never saw the shutdown"
    t.join(5.0)
    assert results == ["Demo"]
    assert not srv.running and not os.path.exists(discovery)


def test_client_reports_a_bridge_that_accepts_but_never_answers(discovery, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    with open(discovery, "w") as fh:
        json.dump({"host": "127.0.0.1", "port": listener.getsockname()[1], "token": "t"}, fh)
    monkeypatch.setattr("renderflow.bridge.client.HANDSHAKE_TIMEOUT_S", 0.3)
    try:
        with pytest.raises(BridgeUnavailable, match="did not answer"):
            Bridge.discover(discovery).connect()
    finally:
        listener.close()


# -------------------------------------------------------------- install
def test_install_fills_in_repo_and_writes_the_launcher(tmp_path):
    from renderflow.bridge.install import SOURCE, install, scripts_dir

    target = install(tmp_path / "Utility", repo=r"D:\work\RenderFlow")
    assert target == tmp_path / "Utility" / "RenderFlow_Bridge.py"
    text = target.read_text("utf-8")
    assert 'REPO = r"D:\\work\\RenderFlow"' in text                # backslashes survive re.sub
    assert text.count("REPO = ") == 1
    compile(text, str(target), "exec")                          # still valid Python
    # the default repo is the checkout the launcher lives in
    assert install(tmp_path / "again").read_text("utf-8").count(f'REPO = r"{SOURCE.parent.parent}"') == 1
    appdata = r"C:\U\me\AppData\Roaming"
    assert scripts_dir("win32", {"APPDATA": appdata}) == (
        Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Support"
        / "Fusion" / "Scripts" / "Utility")
    assert scripts_dir("darwin").name == "Utility" and scripts_dir("linux").name == "Utility"


def test_install_needs_the_checkout(tmp_path):
    from renderflow.bridge.install import install

    with pytest.raises(FileNotFoundError):
        install(tmp_path, source=tmp_path / "missing.py")
