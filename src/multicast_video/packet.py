"""
Custom packet format for multicast video streaming.

Wire layout (network byte order / big-endian):

  Offset  Bytes  Field          Type    Description
  ──────  ─────  ─────────────  ──────  ──────────────────────────────────────
       0      4  magic          4s      b"MVSF" – sanity / version guard
       4      1  version        B       protocol version (currently 1)
       5      4  frame_id       I       monotonically increasing frame counter
       9      2  chunk_index    H       0-based index of this chunk in the frame
      11      2  total_chunks   H       total number of chunks for this frame
      13      8  timestamp_ms   Q       sender wall-clock in milliseconds
      21      N  payload        bytes   JPEG data slice (N ≤ MAX_UDP_PAYLOAD − 21)

The frame_id is the primary sequence number used by the receiver to detect
dropped frames.  Each frame may span multiple UDP datagrams (chunks); the
receiver buffers chunks until all total_chunks have arrived.
"""

import struct
import time
from dataclasses import dataclass

MAGIC: bytes = b"MVSF"
VERSION: int = 1

# fmt: off
_HEADER_FMT  = "!4sBIHHQ"            # big-endian
HEADER_SIZE  = struct.calcsize(_HEADER_FMT)   # 21 bytes
# fmt: on

from .config import MAX_UDP_PAYLOAD

MAX_CHUNK_PAYLOAD: int = MAX_UDP_PAYLOAD - HEADER_SIZE  # 1379 bytes


@dataclass
class VideoPacket:
    """A single UDP datagram carrying one chunk of an encoded video frame."""

    frame_id: int
    chunk_index: int
    total_chunks: int
    timestamp_ms: int
    payload: bytes

    # ------------------------------------------------------------------ #
    # Serialisation                                                        #
    # ------------------------------------------------------------------ #

    def encode(self) -> bytes:
        """Return the wire bytes for this packet."""
        header = struct.pack(
            _HEADER_FMT,
            MAGIC,
            VERSION,
            self.frame_id,
            self.chunk_index,
            self.total_chunks,
            self.timestamp_ms,
        )
        return header + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "VideoPacket":
        """Parse wire bytes into a VideoPacket.

        Raises:
            ValueError: if the data is too short, has a bad magic, or uses an
                        unsupported protocol version.
        """
        if len(data) < HEADER_SIZE:
            raise ValueError(
                f"Packet too short: got {len(data)} bytes, need at least {HEADER_SIZE}"
            )

        magic, version, frame_id, chunk_index, total_chunks, timestamp_ms = struct.unpack(
            _HEADER_FMT, data[:HEADER_SIZE]
        )

        if magic != MAGIC:
            raise ValueError(f"Bad magic: expected {MAGIC!r}, got {magic!r}")
        if version != VERSION:
            raise ValueError(f"Unsupported protocol version: {version}")
        if total_chunks == 0:
            raise ValueError("total_chunks must be ≥ 1")
        if chunk_index >= total_chunks:
            raise ValueError(
                f"chunk_index {chunk_index} is out of range for total_chunks {total_chunks}"
            )

        return cls(
            frame_id=frame_id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            timestamp_ms=timestamp_ms,
            payload=data[HEADER_SIZE:],
        )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


_MAX_TOTAL_CHUNKS: int = 0xFFFF  # uint16 ceiling

def chunk_frame(
    jpeg_data: bytes,
    frame_id: int,
    timestamp_ms: int | None = None,
) -> list[VideoPacket]:
    """Split a JPEG-encoded frame into a list of UDP-sized VideoPackets.

    Args:
        jpeg_data:    Raw JPEG bytes to transmit.
        frame_id:     Sequence number assigned by the sender (0-based, wraps at 2³²).
        timestamp_ms: Sender timestamp in milliseconds.  Defaults to ``time.time() * 1000``.

    Returns:
        Ordered list of VideoPacket objects; each encodes to ≤ MAX_UDP_PAYLOAD bytes.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    # Guarantee at least one packet even for an empty payload.
    if not jpeg_data:
        return [
            VideoPacket(
                frame_id=frame_id,
                chunk_index=0,
                total_chunks=1,
                timestamp_ms=timestamp_ms,
                payload=b"",
            )
        ]

    chunks = [
        jpeg_data[i : i + MAX_CHUNK_PAYLOAD]
        for i in range(0, len(jpeg_data), MAX_CHUNK_PAYLOAD)
    ]
    total = len(chunks)
    if total > _MAX_TOTAL_CHUNKS:
        raise ValueError(
            f"Frame too large: {len(jpeg_data)} bytes requires {total} chunks, "
            f"but the uint16 header field supports at most {_MAX_TOTAL_CHUNKS}. "
            f"Reduce JPEG quality or increase MAX_UDP_PAYLOAD."
        )
    return [
        VideoPacket(
            frame_id=frame_id,
            chunk_index=idx,
            total_chunks=total,
            timestamp_ms=timestamp_ms,
            payload=chunk,
        )
        for idx, chunk in enumerate(chunks)
    ]
