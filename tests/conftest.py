"""
Shared pytest fixtures for the multicast_video test suite.
"""

import sys
import os

# Ensure the src/ layout package is importable without installing.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import struct
import pytest
import numpy as np

from multicast_video.packet import (
    MAGIC,
    VERSION,
    HEADER_SIZE,
    MAX_CHUNK_PAYLOAD,
    VideoPacket,
    chunk_frame,
)


@pytest.fixture
def small_jpeg() -> bytes:
    """A minimal valid JPEG payload (~100 bytes) for unit tests."""
    import cv2

    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok, "cv2.imencode failed in fixture"
    return buf.tobytes()


@pytest.fixture
def large_jpeg() -> bytes:
    """A JPEG payload larger than MAX_CHUNK_PAYLOAD to exercise chunking."""
    import cv2

    # 640×480 white frame encodes to ~20-50 KB.
    frame = np.full((480, 640, 3), 200, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    data = buf.tobytes()
    assert len(data) > MAX_CHUNK_PAYLOAD, (
        f"Expected JPEG > {MAX_CHUNK_PAYLOAD} bytes for chunking test, got {len(data)}"
    )
    return data


@pytest.fixture
def sample_packet() -> VideoPacket:
    return VideoPacket(
        frame_id=42,
        chunk_index=0,
        total_chunks=1,
        timestamp_ms=1_700_000_000_000,
        payload=b"hello",
    )
