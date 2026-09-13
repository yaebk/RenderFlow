"""RenderFlow bridge - run this from inside DaVinci Resolve.

============================================================================
INSTALL
============================================================================
1. Edit REPO below to point at your RenderFlow checkout.
2. Copy this file to Resolve's script menu folder:

     %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\

3. In Resolve: Workspace -> Scripts -> RenderFlow_Bridge

A small window appears saying the bridge is listening. Leave it open. From
any terminal:

     python -m renderflow.bridge

should print your Resolve version, project and timeline. Close the window
(or run ``python -m renderflow.bridge --shutdown``) to stop it.

============================================================================
WHAT IT DOES
============================================================================
The free edition of Resolve lets nothing outside the app use the scripting
API. This script runs *inside* Resolve, where the API is available, and
relays calls from outside over a localhost socket. Only programs on this
machine can reach it, and every request must carry a token that is written
to ~/.renderflow/bridge.json when the bridge starts.

Works on Studio too, but there `renderflow.connect()` attaches directly and
this script is unnecessary.
"""

import sys

# ==========================================================================
# CONFIG
# ==========================================================================
REPO = r"C:\Users\snake\OneDrive\Documents\GitHub\RenderFlow"
PORT = 0            # 0 = any free port; clients find it via the discovery file
# ==========================================================================

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from renderflow.bridge.server import BridgeServer          # noqa: E402


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

    stopping = []

    def stop(ev=None):
        stopping.append(True)

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
    except Exception as exc:                                     # noqa: BLE001
        print("status window unavailable (%s: %s)" % (type(exc).__name__, exc))
        print("bridge is still running without a window.")
        print("stop it with:  python -m renderflow.bridge --shutdown")
        server.serve_forever()
    finally:
        server.stop()
        print("RenderFlow bridge stopped.")


main()
