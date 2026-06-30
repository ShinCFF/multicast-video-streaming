"""
MulticastVideoClient – joins a multicast group, receives UDP chunks, reassembles
frames, displays video in real time, and reports packet-loss statistics.

Receive pipeline
────────────────
  UDP recvfrom()  ──▶  VideoPacket.decode()  ──▶  FrameBuffer  ──▶  cv2.imdecode()
                                                   (chunk reassembly)

Loss detection
──────────────
The server stamps each frame with a monotonically increasing frame_id.  The
client tracks the highest seen frame_id and counts gaps to estimate how many
frames were lost in transit.

Threading model
───────────────
Two threads share a thread-safe queue:

  ┌─────────────┐  queue  ┌──────────────┐
  │ receive_loop│ ──────▶ │ display_loop │
  └─────────────┘         └──────────────┘

This prevents the display (imshow / waitKey) from blocking the UDP receive
buffer and causing OS-level packet drops.
"""

import logging
import queue
import socket
import threading
import time

import cv2
import numpy as np

from .config import (
    DEFAULT_INTERFACE,
    FRAME_BUFFER_TIMEOUT_S,
    MULTICAST_GROUP,
    MULTICAST_PORT,
    STATS_INTERVAL_S,
    UDP_RECV_BUFFER,
)
from .packet import VideoPacket
from .socket_utils import create_receiver_socket, leave_multicast_group

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Internal helper: per-frame chunk accumulator
# ────────────────────────────────────────────────────────────────────────────


class _FrameBuffer:
    """Accumulates UDP chunks for a single video frame."""

    def __init__(self, total_chunks: int, timestamp_ms: int) -> None:
        self.total_chunks = total_chunks
        self.timestamp_ms = timestamp_ms
        self.chunks: dict[int, bytes] = {}
        self._created_at = time.monotonic()

    def add_chunk(self, index: int, payload: bytes) -> None:
        self.chunks[index] = payload

    @property
    def is_complete(self) -> bool:
        return len(self.chunks) == self.total_chunks

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._created_at

    def assemble(self) -> bytes:
        """Concatenate chunks in order to reconstruct the JPEG payload."""
        return b"".join(self.chunks[i] for i in range(self.total_chunks))


# ────────────────────────────────────────────────────────────────────────────
# Loss statistics tracker
# ────────────────────────────────────────────────────────────────────────────


class _LossTracker:
    """Tracks received vs expected frame_ids to compute packet-loss rate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_expected: int | None = None
        self._received: int = 0
        self._lost: int = 0
        self._last_log = time.monotonic()

    def record(self, frame_id: int) -> None:
        with self._lock:
            if self._next_expected is None:
                self._next_expected = frame_id

            # Use modular arithmetic to correctly handle 32-bit wrap-around.
            # A gap > 0x8000_0000 means the frame arrived late / out-of-order;
            # in that case we do NOT advance _next_expected backward.
            gap = (frame_id - self._next_expected) & 0xFFFF_FFFF
            if gap == 0:
                # Exact match – no loss.
                pass
            elif gap < 0x8000_0000:
                # Forward gap: frames were lost.
                self._lost += gap
                logger.debug(
                    "Gap detected: expected %d, got %d (lost %d)",
                    self._next_expected,
                    frame_id,
                    gap,
                )
            else:
                # Backward gap: late or duplicate packet – ignore, do not move cursor.
                logger.debug("Late/duplicate packet: frame_id=%d (expected %d)", frame_id, self._next_expected)
                self._received += 1
                return

            self._next_expected = (frame_id + 1) & 0xFFFF_FFFF
            self._received += 1

            if time.monotonic() - self._last_log >= STATS_INTERVAL_S:
                self._log()
                self._last_log = time.monotonic()

    def _log(self) -> None:
        total = self._received + self._lost
        rate = (self._lost / total * 100) if total else 0.0
        logger.info(
            "Statistics – received: %d  lost: %d  loss rate: %.1f%%",
            self._received,
            self._lost,
            rate,
        )

    def summary(self) -> dict:
        with self._lock:
            total = self._received + self._lost
            return {
                "received": self._received,
                "lost": self._lost,
                "loss_rate_pct": (self._lost / total * 100) if total else 0.0,
            }


# ────────────────────────────────────────────────────────────────────────────
# Public client class
# ────────────────────────────────────────────────────────────────────────────


class MulticastVideoClient:
    """Receives and displays a multicast video stream.

    Usage::

        with MulticastVideoClient() as client:
            client.display()      # blocks until 'q' is pressed or stream ends

    Args:
        multicast_group:  Multicast group to join (must match the server).
        port:             UDP port to listen on.
        interface_ip:     Local NIC to use.  ``"0.0.0.0"`` = OS default.
        recv_timeout_s:   How long to wait for a packet before giving up.
        frame_queue_size: Maximum number of decoded frames to buffer for display.
    """

    def __init__(
        self,
        multicast_group: str = MULTICAST_GROUP,
        port: int = MULTICAST_PORT,
        interface_ip: str = DEFAULT_INTERFACE,
        recv_timeout_s: float = 10.0,
        frame_queue_size: int = 8,
    ) -> None:
        self.multicast_group = multicast_group
        self.port = port
        self.interface_ip = interface_ip
        self.recv_timeout_s = recv_timeout_s

        self._sock: socket.socket | None = None
        self._frame_buffers: dict[int, _FrameBuffer] = {}
        self._loss_tracker = _LossTracker()
        self._frame_queue: queue.Queue = queue.Queue(maxsize=frame_queue_size)
        self._stop_event = threading.Event()
        self._last_eviction = time.monotonic()

    # ------------------------------------------------------------------ #
    # Context-manager interface                                            #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "MulticastVideoClient":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Create socket and join the multicast group."""
        self._sock = create_receiver_socket(
            self.multicast_group, self.port, self.interface_ip
        )
        self._sock.settimeout(self.recv_timeout_s)
        logger.info("Client joined %s:%d", self.multicast_group, self.port)

    def close(self) -> None:
        """Leave the multicast group and release resources."""
        self._stop_event.set()
        if self._sock is not None:
            leave_multicast_group(self._sock, self.multicast_group, self.interface_ip)
            self._sock.close()
            self._sock = None
        self._frame_buffers.clear()
        stats = self._loss_tracker.summary()
        logger.info(
            "Session ended – received: %(received)d  lost: %(lost)d  "
            "loss rate: %(loss_rate_pct).1f%%",
            stats,
        )

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def display(self, window_title: str = "Multicast Video") -> None:
        """Display the incoming stream until *q* is pressed or the stream times out.

        Spawns a background receive thread and runs the OpenCV display loop on
        the calling thread (required by OpenCV on most platforms).
        """
        if self._sock is None:
            raise RuntimeError("Call open() or use the client as a context manager.")

        recv_thread = threading.Thread(
            target=self._receive_loop, name="recv", daemon=True
        )
        recv_thread.start()

        self._display_loop(window_title)

        self._stop_event.set()
        recv_thread.join(timeout=2.0)

    def receive_frame(self) -> np.ndarray | None:
        """Block until one complete frame is decoded.

        Returns:
            A BGR ``numpy.ndarray`` frame, or ``None`` if the socket times out.
        """
        if self._sock is None:
            raise RuntimeError("Call open() or use the client as a context manager.")

        while not self._stop_event.is_set():
            try:
                raw, _ = self._sock.recvfrom(UDP_RECV_BUFFER)
            except socket.timeout:
                return None

            frame = self._process_datagram(raw)
            if frame is not None:
                return frame
        return None

    # ------------------------------------------------------------------ #
    # Internal: receive / display loops                                    #
    # ------------------------------------------------------------------ #

    def _receive_loop(self) -> None:
        """Background thread: receive datagrams and push complete frames to the queue."""
        while not self._stop_event.is_set():
            try:
                raw, _ = self._sock.recvfrom(UDP_RECV_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                break

            frame = self._process_datagram(raw)
            if frame is not None:
                if self._frame_queue.full():
                    # Drop the oldest frame to stay live rather than stalling.
                    try:
                        self._frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self._frame_queue.put_nowait(frame)
                except queue.Full:
                    pass  # display thread drained and re-filled in the same moment

    def _display_loop(self, window_title: str) -> None:
        """Main thread: dequeue frames and show them with OpenCV."""
        cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
        try:
            while not self._stop_event.is_set():
                try:
                    frame = self._frame_queue.get(timeout=self.recv_timeout_s)
                except queue.Empty:
                    logger.warning("No frame received within %.1fs – stream may have ended.", self.recv_timeout_s)
                    break

                stats = self._loss_tracker.summary()
                self._overlay_stats(frame, stats)
                cv2.imshow(window_title, frame)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
        finally:
            cv2.destroyAllWindows()

    # ------------------------------------------------------------------ #
    # Internal: datagram processing                                        #
    # ------------------------------------------------------------------ #

    def _process_datagram(self, raw: bytes) -> np.ndarray | None:
        """Parse one UDP datagram and return a decoded frame if the frame is now complete."""
        try:
            pkt = VideoPacket.decode(raw)
        except ValueError as exc:
            logger.debug("Discarding invalid datagram: %s", exc)
            return None

        buf = self._frame_buffers.get(pkt.frame_id)
        if buf is None:
            buf = _FrameBuffer(pkt.total_chunks, pkt.timestamp_ms)
            self._frame_buffers[pkt.frame_id] = buf
            self._maybe_evict_stale_buffers()

        buf.add_chunk(pkt.chunk_index, pkt.payload)

        if buf.is_complete:
            del self._frame_buffers[pkt.frame_id]
            self._loss_tracker.record(pkt.frame_id)
            return self._decode_jpeg(buf.assemble(), pkt.frame_id)

        return None

    def _decode_jpeg(self, jpeg_data: bytes, frame_id: int) -> np.ndarray | None:
        arr = np.frombuffer(jpeg_data, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            logger.warning("JPEG decode failed for frame %d", frame_id)
        return frame

    _EVICTION_INTERVAL_S: float = 0.5  # run the scan at most twice per second

    def _maybe_evict_stale_buffers(self) -> None:
        """Evict incomplete frame buffers, rate-limited to avoid O(n) scan per packet."""
        now = time.monotonic()
        if now - self._last_eviction < self._EVICTION_INTERVAL_S:
            return
        self._last_eviction = now
        stale = [fid for fid, buf in self._frame_buffers.items() if buf.age_s > FRAME_BUFFER_TIMEOUT_S]
        for fid in stale:
            logger.debug("Evicting stale frame buffer for frame_id=%d", fid)
            del self._frame_buffers[fid]

    # ------------------------------------------------------------------ #
    # Internal: display helpers                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _overlay_stats(frame: np.ndarray, stats: dict) -> None:
        """Draw a semi-transparent statistics overlay on *frame* in place."""
        text = (
            f"Rcv: {stats['received']}  "
            f"Lost: {stats['lost']}  "
            f"Loss: {stats['loss_rate_pct']:.1f}%"
        )
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(frame, (8, 8), (tw + 16, th + 20), (0, 0, 0), -1)
        cv2.putText(frame, text, (12, th + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)
