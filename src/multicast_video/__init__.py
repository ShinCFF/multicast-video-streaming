"""
multicast_video – UDP multicast MJPEG video streaming library.

Public API::

    from multicast_video import MulticastVideoServer, MulticastVideoClient

See :mod:`multicast_video.server` and :mod:`multicast_video.client` for full
documentation.
"""

from .client import MulticastVideoClient
from .server import MulticastVideoServer

__all__ = ["MulticastVideoServer", "MulticastVideoClient"]
__version__ = "1.0.0"
