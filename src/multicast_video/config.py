"""Shared constants for the multicast video streaming application."""

MULTICAST_GROUP = "239.1.1.1"
MULTICAST_PORT = 5004
TTL = 1                       # 1 = LAN only; increase to cross routers
DEFAULT_INTERFACE = "0.0.0.0"

TARGET_FPS = 20
FRAME_INTERVAL_S = 1.0 / TARGET_FPS  # 50 ms

JPEG_QUALITY = 80             # 0-100; lower = smaller packets, more artifacts
MAX_UDP_PAYLOAD = 1400        # bytes; conservative for Ethernet MTU of 1500
UDP_RECV_BUFFER = 65536       # 64 KB socket receive buffer
FRAME_BUFFER_TIMEOUT_S = 2.0  # discard incomplete frames older than this
STATS_INTERVAL_S = 5.0        # how often to log loss statistics
