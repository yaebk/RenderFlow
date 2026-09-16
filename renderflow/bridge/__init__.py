"""Reach the DaVinci Resolve scripting API from outside Resolve, on any edition.

The free edition of Resolve has no external scripting: nothing outside the
application can attach to it. Scripts run from *inside* Resolve, however, get
the full API. So the bridge turns that around: a small server started from
Workspace -> Scripts listens on localhost and relays API calls from any
outside program.

    outside process  --JSON over TCP-->  bridge (inside Resolve)  -->  resolve API

Two halves:

* :mod:`renderflow.bridge.server` runs inside Resolve. Standard library only
  and nothing newer than Python 3.7, because it runs on whatever Python
  Resolve embeds.
* :mod:`renderflow.bridge.client` runs anywhere. It gives you an object that
  behaves like the real ``resolve`` object, so code written against the
  Resolve API works unchanged over the bridge.

On Resolve Studio, :func:`renderflow.bridge.client.connect` attaches directly
and never touches the bridge.
"""

from renderflow.bridge.client import Bridge, BridgeUnavailable, RemoteError, connect
from renderflow.bridge.server import BridgeServer

__all__ = ["Bridge", "BridgeServer", "BridgeUnavailable", "RemoteError", "connect"]
