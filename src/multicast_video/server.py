"""
MulticastVideoServer – reads an MJPEG/video file and streams it to a multicast
group at a fixed frame rate, looping indefinitely.

Streaming pipeline
──────────────────
  VideoCapture  ──▶  JPEG encode  ──▶  chunk_frame()  ──▶  UDP sendto()
  (cv2)                (cv2)              (packet.py)       (multicast)

Each frame is assigned a monotonically increasing frame_id (sequence number)
so receivers can detect dropped frames.
"""

import logging
import socket
import time
from typing import Optional

import cv2
import numpy as np

from .config import (
    FRAME_INTERVAL_S,
    JPEG_QUALITY,
    MULTICAST_GROUP,
    MULTICAST_PORT,
    TARGET_FPS,
    TTL,
)
from .packet import chunk_frame
from .socket_utils import create_sender_socket

logger = logging.getLogger(__name__)


class MulticastVideoServer:
    """Streams an MJPEG/video file to a multicast group.

    Usage::

        with MulticastVideoServer("video.mjpeg") as srv:
            srv.stream()          # blocks until KeyboardInterrupt or error

    Args:
        video_path:       Path to the source video file (any format OpenCV can read).
        multicast_group:  Destination multicast IP address.
        port:             Destination UDP port.
        ttl:              Multicast TTL hop limit.
        interface_ip:     Source NIC IP; ``"0.0.0.0"`` = OS default.
        fps:              Target frames per second.  Defaults to :data:`TARGET_FPS`.
        jpeg_quality:     JPEG encode quality 0-100.
    """

    def __init__(
        self,
        video_path: str,
        multicast_group: str = MULTICAST_GROUP,
        port: int = MULTICAST_PORT,
        ttl: int = TTL,
        interface_ip: str = "0.0.0.0",
        fps: float = TARGET_FPS,
        jpeg_quality: int = JPEG_QUALITY,
    ) -> None:
        self.video_path = video_path
        self.multicast_group = multicast_group
        self.port = port
        self.ttl = ttl
        self.interface_ip = interface_ip
        self.fps = fps
        self.frame_interval = 1.0 / fps
        self.jpeg_quality = jpeg_quality

        self._sock: Optional[socket.socket] = None
        self._cap: Optional[cv2.VideoCapture] = None
        self._frame_id: int = 0

    # ------------------------------------------------------------------ #
    # Context-manager interface                                            #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "MulticastVideoServer":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Open the video file and multicast socket."""
        self._cap = cv2.VideoCapture(self.video_path)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open video file: {self.video_path!r}")

        self._sock = create_sender_socket(self.interface_ip, self.ttl)
        logger.info(
            "Server ready: %s → %s:%d @ %.1f fps",
            self.video_path,
            self.multicast_group,
            self.port,
            self.fps,
        )

    def close(self) -> None:
        """Release the video capture and socket resources."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # ------------------------------------------------------------------ #
    # Streaming                                                            #
    # ------------------------------------------------------------------ #

    def stream(self) -> None:
        """Stream the video file in a loop until interrupted.

        Reads frames sequentially, restarts from the beginning when the file
        ends (as required by the project specification), and paces output to
        :attr:`fps` using monotonic sleep.

        Raises:
            RuntimeError: if :meth:`open` has not been called first.
        """
        if self._sock is None or self._cap is None:
            raise RuntimeError("Call open() or use the server as a context manager.")

        endpoint = (self.multicast_group, self.port)
        logger.info("Streaming started.  Press Ctrl-C to stop.")

        try:
            while True:
                t_start = time.monotonic()

                ret, frame = self._cap.read()
                if not ret:
                    # End of file – restart.  Reset t_start so the timing
                    # calculation on the next iteration is not stale.
                    logger.debug("EOF reached, looping.")
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    t_start = time.monotonic()
                    continue

                self._send_frame(frame, endpoint)

                # Pace to the target frame rate.
                elapsed = time.monotonic() - t_start
                sleep_for = self.frame_interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)

        except KeyboardInterrupt:
            logger.info("Streaming stopped by user.")

    def _send_frame(self, frame: np.ndarray, endpoint: tuple) -> None:
        """JPEG-encode *frame* and transmit all its UDP chunks."""
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        if not ok:
            logger.warning("JPEG encode failed for frame %d – skipping.", self._frame_id)
            return

        packets = chunk_frame(buf.tobytes(), self._frame_id)
        for pkt in packets:
            self._sock.sendto(pkt.encode(), endpoint)

        logger.debug(
            "Sent frame %d: %d bytes in %d chunk(s)",
            self._frame_id,
            len(buf),
            len(packets),
        )

        # Wrap at 32 bits to match the uint32 field in the packet header.
        self._frame_id = (self._frame_id + 1) & 0xFFFF_FFFF
