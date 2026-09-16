"""RenderFlow bridge - run this from inside DaVinci Resolve.

Install with ``python -m renderflow install-bridge``, which copies this file
into Resolve's Utility scripts folder with REPO filled in. Then, in Resolve:
Workspace -> Scripts -> RenderFlow_Bridge.

A small window says the bridge is listening; leave it open. From a terminal,
``python -m renderflow.bridge`` should print your Resolve version, project and
timeline. Close the window, or run ``python -m renderflow.bridge --shutdown``,
to stop it.

The free edition of Resolve lets nothing outside the app use the scripting
API. This script runs inside Resolve, where the API is available, and relays
calls from outside over a localhost socket. Only programs on this machine can
reach it, and every request must carry the token written to
~/.renderflow/bridge.json when the bridge starts. On Studio,
``renderflow.connect()`` attaches directly and this script is not needed.
"""

import sys

REPO = ""           # your RenderFlow checkout; install-bridge fills this in
PORT = 0            # 0 = any free port; clients find it via the discovery file

if REPO and REPO not in sys.path:
    sys.path.insert(0, REPO)

try:
    from renderflow.bridge.server import BridgeServer
except ImportError:
    raise SystemExit("RenderFlow bridge: cannot import renderflow. Install this script with "
                     "'python -m renderflow install-bridge' from your RenderFlow checkout, "
                     "or set REPO at the top of it.")


def get_resolve():
    handle = globals().get("resolve")
    if handle is not None:
        return handle
    bmd = get_bmd()
    if bmd is not None:
        return bmd.scriptapp("Resolve")
    return None


def get_bmd():
    handle = globals().get("bmd")
    if handle is not None:
        return handle
    try:
        import fusionscript                                  # the in-app module
        return fusionscript
    except ImportError:
        return None


def run_with_window(server, resolve):
    """Keep Resolve responsive: pump the UI toolkit's events and the socket in turn.

    Resolve's UI event loop (``RunLoop``) never yields to other Python threads,
    so the socket is serviced from this same thread, one step at a time.
    """
    bmd = get_bmd()
    fusion = globals().get("fusion") or resolve.Fusion()
    ui = fusion.UIManager
    disp = bmd.UIDispatcher(ui)
    if not hasattr(disp, "StepLoop"):
        raise RuntimeError("UIDispatcher has no StepLoop")

    host, port = server.address
    win = disp.AddWindow(
        {"ID": "RenderFlowBridge", "WindowTitle": "RenderFlow Bridge",
         "Geometry": [200, 200, 440, 150]},
        ui.VGroup({"Spacing": 8}, [
            ui.Label({"ID": "Status", "WordWrap": True,
                      "Text": "Listening on %s:%d\n\nLeave this window open. "
                              "From a terminal:  python -m renderflow.bridge" % (host, port)}),
            ui.Button({"ID": "Stop", "Text": "Stop bridge"}),
        ]),
    )

    stopping = False

    def stop(ev=None):
        nonlocal stopping
        stopping = True

    win.On.Stop.Clicked = stop
    win.On.RenderFlowBridge.Close = stop

    win.Show()
    try:
        while not stopping and server.poll(0.05):
            disp.StepLoop()
    finally:
        win.Hide()


def main():
    resolve = get_resolve()
    if resolve is None:
        print("RenderFlow bridge: could not find the resolve object - run this from "
              "Workspace -> Scripts inside DaVinci Resolve.")
        return

    server = BridgeServer(resolve, port=PORT)
    host, port = server.listen()
    print("RenderFlow bridge listening on %s:%d" % (host, port))
    print("discovery file: %s" % server.discovery_path)

    try:
        run_with_window(server, resolve)
    except Exception as exc:
        print("status window unavailable (%s: %s)" % (type(exc).__name__, exc))
        print("bridge is still running without a window.")
        print("stop it with:  python -m renderflow.bridge --shutdown")
        server.serve_forever()
    finally:
        server.stop()
        print("RenderFlow bridge stopped.")


main()
