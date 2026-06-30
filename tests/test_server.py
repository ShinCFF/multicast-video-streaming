"""
Tests for multicast_video.server.MulticastVideoServer.

OpenCV VideoCapture is mocked so these tests run without a real video file
or network interface.
"""

from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

from multicast_video.server import MulticastVideoServer


GROUP = "239.1.1.1"
PORT = 5004


def _make_server(**kwargs) -> MulticastVideoServer:
    defaults = dict(video_path="fake.mjpeg", multicast_group=GROUP, port=PORT)
    defaults.update(kwargs)
    return MulticastVideoServer(**defaults)


# ────────────────────────────────────────────────────────────────────────────
# open / close
# ────────────────────────────────────────────────────────────────────────────


@patch("multicast_video.server.create_sender_socket")
@patch("multicast_video.server.cv2.VideoCapture")
class TestServerLifecycle:
    def test_open_creates_capture_and_socket(self, MockCap, MockSocket):
        MockCap.return_value.isOpened.return_value = True
        srv = _make_server()
        srv.open()
        MockCap.assert_called_once_with("fake.mjpeg")
        MockSocket.assert_called_once()
        srv.close()

    def test_open_raises_if_file_unreadable(self, MockCap, MockSocket):
        MockCap.return_value.isOpened.return_value = False
        srv = _make_server()
        with pytest.raises(RuntimeError, match="Cannot open"):
            srv.open()

    def test_close_releases_capture(self, MockCap, MockSocket):
        MockCap.return_value.isOpened.return_value = True
        srv = _make_server()
        srv.open()
        srv.close()
        MockCap.return_value.release.assert_called_once()

    def test_close_closes_socket(self, MockCap, MockSocket):
        MockCap.return_value.isOpened.return_value = True
        srv = _make_server()
        srv.open()
        srv.close()
        MockSocket.return_value.close.assert_called_once()

    def test_context_manager_opens_and_closes(self, MockCap, MockSocket):
        MockCap.return_value.isOpened.return_value = True
        with _make_server():
            pass
        MockCap.return_value.release.assert_called_once()


# ────────────────────────────────────────────────────────────────────────────
# _send_frame
# ────────────────────────────────────────────────────────────────────────────


@patch("multicast_video.server.create_sender_socket")
@patch("multicast_video.server.cv2.VideoCapture")
class TestSendFrame:
    def _open_server(self, MockCap, MockSocket):
        MockCap.return_value.isOpened.return_value = True
        srv = _make_server()
        srv.open()
        return srv, MockSocket.return_value

    def test_sendto_is_called(self, MockCap, MockSocket):
        srv, mock_sock = self._open_server(MockCap, MockSocket)
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        srv._send_frame(frame, (GROUP, PORT))
        assert mock_sock.sendto.called
        srv.close()

    def test_frame_id_increments(self, MockCap, MockSocket):
        srv, _ = self._open_server(MockCap, MockSocket)
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        srv._send_frame(frame, (GROUP, PORT))
        srv._send_frame(frame, (GROUP, PORT))
        assert srv._frame_id == 2
        srv.close()

    def test_frame_id_wraps_at_32_bits(self, MockCap, MockSocket):
        srv, _ = self._open_server(MockCap, MockSocket)
        srv._frame_id = 0xFFFF_FFFF
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        srv._send_frame(frame, (GROUP, PORT))
        assert srv._frame_id == 0
        srv.close()

    def test_all_packets_sent_to_correct_endpoint(self, MockCap, MockSocket):
        srv, mock_sock = self._open_server(MockCap, MockSocket)
        frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        srv._send_frame(frame, (GROUP, PORT))
        for c in mock_sock.sendto.call_args_list:
            assert c.args[1] == (GROUP, PORT)
        srv.close()


# ────────────────────────────────────────────────────────────────────────────
# stream – early-exit via KeyboardInterrupt
# ────────────────────────────────────────────────────────────────────────────


@patch("multicast_video.server.time.sleep")
@patch("multicast_video.server.create_sender_socket")
@patch("multicast_video.server.cv2.VideoCapture")
class TestStream:
    def test_stream_raises_without_open(self, MockCap, MockSocket, MockSleep):
        srv = _make_server()
        with pytest.raises(RuntimeError):
            srv.stream()

    def test_send_frame_skips_on_bad_jpeg_encode(self, MockCap, MockSocket, MockSleep):
        MockCap.return_value.isOpened.return_value = True
        srv = _make_server()
        srv.open()
        mock_sock = MockSocket.return_value
        with patch("multicast_video.server.cv2.imencode", return_value=(False, None)):
            srv._send_frame(np.zeros((32, 32, 3), dtype=np.uint8), (GROUP, PORT))
        mock_sock.sendto.assert_not_called()
        srv.close()

    def test_stream_loops_on_eof_then_stops(self, MockCap, MockSocket, MockSleep):
        """Simulate: first read returns a frame, second returns EOF (restart),
        third raises KeyboardInterrupt to exit."""
        MockCap.return_value.isOpened.return_value = True
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        MockCap.return_value.read.side_effect = [
            (True, frame),
            (False, None),         # EOF → loop
            KeyboardInterrupt,     # exit
        ]
        srv = _make_server()
        srv.open()
        srv.stream()   # should not raise
        MockCap.return_value.set.assert_called()   # seek back to 0
        srv.close()
