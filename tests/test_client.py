"""
Tests for multicast_video.client – FrameBuffer, LossTracker, and
MulticastVideoClient (socket mocked; no real network required).
"""

import socket
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from multicast_video.client import (
    MulticastVideoClient,
    _FrameBuffer,
    _LossTracker,
)
from multicast_video.packet import VideoPacket, chunk_frame


GROUP = "239.1.1.1"
PORT = 5004


# ────────────────────────────────────────────────────────────────────────────
# _FrameBuffer
# ────────────────────────────────────────────────────────────────────────────


class TestFrameBuffer:
    def test_not_complete_initially(self):
        buf = _FrameBuffer(total_chunks=3, timestamp_ms=0)
        assert not buf.is_complete

    def test_complete_when_all_chunks_added(self):
        buf = _FrameBuffer(total_chunks=2, timestamp_ms=0)
        buf.add_chunk(0, b"aaa")
        buf.add_chunk(1, b"bbb")
        assert buf.is_complete

    def test_not_complete_with_missing_chunk(self):
        buf = _FrameBuffer(total_chunks=3, timestamp_ms=0)
        buf.add_chunk(0, b"aaa")
        buf.add_chunk(2, b"ccc")
        assert not buf.is_complete

    def test_assemble_restores_order(self):
        buf = _FrameBuffer(total_chunks=3, timestamp_ms=0)
        buf.add_chunk(2, b"ccc")
        buf.add_chunk(0, b"aaa")
        buf.add_chunk(1, b"bbb")
        assert buf.assemble() == b"aaabbbccc"

    def test_single_chunk_assemble(self):
        buf = _FrameBuffer(total_chunks=1, timestamp_ms=0)
        buf.add_chunk(0, b"hello")
        assert buf.assemble() == b"hello"

    def test_age_increases_over_time(self):
        buf = _FrameBuffer(total_chunks=1, timestamp_ms=0)
        time.sleep(0.05)
        assert buf.age_s >= 0.04


# ────────────────────────────────────────────────────────────────────────────
# _LossTracker
# ────────────────────────────────────────────────────────────────────────────


class TestLossTracker:
    def test_no_loss_sequential(self):
        tracker = _LossTracker()
        for fid in range(5):
            tracker.record(fid)
        stats = tracker.summary()
        assert stats["received"] == 5
        assert stats["lost"] == 0
        assert stats["loss_rate_pct"] == 0.0

    def test_detects_single_gap(self):
        tracker = _LossTracker()
        tracker.record(0)
        tracker.record(3)   # frames 1 and 2 were lost
        stats = tracker.summary()
        assert stats["lost"] == 2

    def test_detects_multiple_gaps(self):
        tracker = _LossTracker()
        tracker.record(0)
        tracker.record(5)   # 4 lost
        tracker.record(7)   # 1 lost
        stats = tracker.summary()
        assert stats["lost"] == 5

    def test_loss_rate_calculation(self):
        tracker = _LossTracker()
        tracker.record(0)
        tracker.record(2)   # 1 lost
        stats = tracker.summary()
        # received=2, lost=1, total=3 → 33.3%
        assert abs(stats["loss_rate_pct"] - 33.33) < 0.1

    def test_first_frame_sets_baseline(self):
        tracker = _LossTracker()
        tracker.record(100)  # first frame ever; not treated as loss
        stats = tracker.summary()
        assert stats["lost"] == 0

    def test_zero_total_gives_zero_rate(self):
        tracker = _LossTracker()
        assert tracker.summary()["loss_rate_pct"] == 0.0

    def test_out_of_order_frame_does_not_move_cursor_backward(self):
        """A late packet must not reset _next_expected backward (bug #1)."""
        tracker = _LossTracker()
        tracker.record(0)
        tracker.record(5)   # gap: lost 1,2,3,4 → _next_expected = 6
        tracker.record(3)   # late packet: must not move cursor to 4
        tracker.record(7)   # should see gap of 1 (frame 6 lost), not 3
        stats = tracker.summary()
        # Lost: 4 (frames 1-4) + 1 (frame 6) = 5
        assert stats["lost"] == 5

    def test_32bit_wrap_around_handled(self):
        """Frame counter wrapping from 0xFFFFFFFF → 0 must not be treated as loss (bug #2)."""
        tracker = _LossTracker()
        tracker.record(0xFFFF_FFFE)
        tracker.record(0xFFFF_FFFF)
        tracker.record(0)           # wrap-around: expected 0 after 0xFFFFFFFF
        stats = tracker.summary()
        assert stats["lost"] == 0
        assert stats["received"] == 3

    def test_32bit_wrap_gap_detected(self):
        """A real gap across the 32-bit boundary is counted as loss."""
        tracker = _LossTracker()
        tracker.record(0xFFFF_FFFF)
        tracker.record(2)   # skipped 0 and 1 across the wrap
        stats = tracker.summary()
        assert stats["lost"] == 2


# ────────────────────────────────────────────────────────────────────────────
# MulticastVideoClient – unit tests with mocked socket
# ────────────────────────────────────────────────────────────────────────────


def _make_client(**kwargs):
    defaults = dict(multicast_group=GROUP, port=PORT, recv_timeout_s=1.0)
    defaults.update(kwargs)
    return MulticastVideoClient(**defaults)


def _encode_frame(jpeg_data: bytes, frame_id: int) -> list[bytes]:
    """Return wire bytes for all chunks of a frame."""
    return [pkt.encode() for pkt in chunk_frame(jpeg_data, frame_id)]


@patch("multicast_video.client.create_receiver_socket")
@patch("multicast_video.client.leave_multicast_group")
class TestClientLifecycle:
    def test_open_creates_socket(self, mock_leave, MockSocket):
        client = _make_client()
        client.open()
        MockSocket.assert_called_once()
        client.close()

    def test_close_calls_leave(self, mock_leave, MockSocket):
        client = _make_client()
        client.open()
        client.close()
        mock_leave.assert_called_once()

    def test_context_manager(self, mock_leave, MockSocket):
        with _make_client():
            pass
        MockSocket.assert_called_once()
        mock_leave.assert_called_once()

    def test_receive_frame_without_open_raises(self, mock_leave, MockSocket):
        client = _make_client()
        with pytest.raises(RuntimeError):
            client.receive_frame()


@patch("multicast_video.client.create_receiver_socket")
@patch("multicast_video.client.leave_multicast_group")
class TestReceiveFrame:
    def _open_with_datagrams(self, MockLeave, MockSocket, datagrams):
        """Return an open client whose socket returns *datagrams* in sequence."""
        mock_sock = MagicMock()
        MockSocket.return_value = mock_sock
        responses = [(d, ("1.2.3.4", PORT)) for d in datagrams]
        responses.append(socket.timeout)  # signal end
        mock_sock.recvfrom.side_effect = [
            *[(d, ("1.2.3.4", PORT)) for d in datagrams],
            socket.timeout(),
        ]
        client = _make_client()
        client.open()
        return client

    def test_receive_single_chunk_frame(self, MockLeave, MockSocket, small_jpeg):
        packets = _encode_frame(small_jpeg, frame_id=0)
        client = self._open_with_datagrams(MockLeave, MockSocket, packets)
        frame = client.receive_frame()
        assert frame is not None
        assert isinstance(frame, np.ndarray)
        client.close()

    def test_receive_multi_chunk_frame(self, MockLeave, MockSocket, large_jpeg):
        packets = _encode_frame(large_jpeg, frame_id=0)
        assert len(packets) > 1, "Expected multi-chunk frame for this test"
        client = self._open_with_datagrams(MockLeave, MockSocket, packets)
        frame = client.receive_frame()
        assert frame is not None
        client.close()

    def test_returns_none_on_timeout(self, MockLeave, MockSocket):
        mock_sock = MagicMock()
        MockSocket.return_value = mock_sock
        mock_sock.recvfrom.side_effect = socket.timeout()
        client = _make_client()
        client.open()
        frame = client.receive_frame()
        assert frame is None
        client.close()

    def test_invalid_datagrams_are_skipped(self, MockLeave, MockSocket, small_jpeg):
        junk = b"\x00\x01\x02"
        good = _encode_frame(small_jpeg, frame_id=0)
        mock_sock = MagicMock()
        MockSocket.return_value = mock_sock
        mock_sock.recvfrom.side_effect = [
            (junk, ("1.2.3.4", PORT)),
            *[(d, ("1.2.3.4", PORT)) for d in good],
            socket.timeout(),
        ]
        client = _make_client()
        client.open()
        frame = client.receive_frame()
        assert frame is not None
        client.close()


class TestOverlayStats:
    def test_overlay_does_not_raise(self):
        frame = np.zeros((200, 400, 3), dtype=np.uint8)
        stats = {"received": 100, "lost": 5, "loss_rate_pct": 4.76}
        MulticastVideoClient._overlay_stats(frame, stats)  # should not raise

    def test_overlay_modifies_frame(self):
        frame = np.zeros((200, 400, 3), dtype=np.uint8)
        original = frame.copy()
        stats = {"received": 10, "lost": 0, "loss_rate_pct": 0.0}
        MulticastVideoClient._overlay_stats(frame, stats)
        assert not np.array_equal(frame, original), "Overlay should draw on the frame"


class TestDecodeJpeg:
    def test_decode_valid_jpeg_returns_array(self, small_jpeg):
        client = _make_client()
        frame = client._decode_jpeg(small_jpeg, frame_id=0)
        assert frame is not None
        assert isinstance(frame, np.ndarray)

    def test_decode_invalid_data_returns_none(self):
        client = _make_client()
        frame = client._decode_jpeg(b"not-jpeg-data", frame_id=0)
        assert frame is None


class TestLossTrackerLog:
    def test_log_fires_when_interval_exceeded(self):
        tracker = _LossTracker()
        tracker._last_log -= 100  # force interval elapsed
        # Record a frame; _log() should be called internally without error.
        tracker.record(0)
        stats = tracker.summary()
        assert stats["received"] == 1


@patch("multicast_video.client.create_receiver_socket")
@patch("multicast_video.client.leave_multicast_group")
class TestEvictStaleBuffers:
    def test_stale_buffers_are_removed(self, MockLeave, MockSocket):
        mock_sock = MagicMock()
        MockSocket.return_value = mock_sock
        mock_sock.recvfrom.side_effect = socket.timeout()

        client = _make_client()
        client.open()

        # Manually insert a stale buffer and force last_eviction to be old.
        from multicast_video.client import _FrameBuffer
        stale = _FrameBuffer(total_chunks=3, timestamp_ms=0)
        stale._created_at = time.monotonic() - 10.0
        client._frame_buffers[999] = stale
        client._last_eviction = time.monotonic() - 10.0  # bypass rate limit

        client._maybe_evict_stale_buffers()
        assert 999 not in client._frame_buffers
        client.close()

    def test_eviction_is_rate_limited(self, MockLeave, MockSocket):
        mock_sock = MagicMock()
        MockSocket.return_value = mock_sock
        mock_sock.recvfrom.side_effect = socket.timeout()

        client = _make_client()
        client.open()

        from multicast_video.client import _FrameBuffer
        stale = _FrameBuffer(total_chunks=3, timestamp_ms=0)
        stale._created_at = time.monotonic() - 10.0
        client._frame_buffers[999] = stale
        # last_eviction is 'now', so rate limit prevents scanning
        client._last_eviction = time.monotonic()

        client._maybe_evict_stale_buffers()
        # Buffer should still be there because eviction was skipped
        assert 999 in client._frame_buffers
        client.close()
