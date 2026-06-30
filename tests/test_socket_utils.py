"""
Tests for multicast_video.socket_utils – socket creation and group membership.

All tests mock the underlying socket so they run without a real network.
"""

import socket
import struct
from unittest.mock import MagicMock, call, patch

import pytest

from multicast_video.socket_utils import (
    create_receiver_socket,
    create_sender_socket,
    leave_multicast_group,
)


# ────────────────────────────────────────────────────────────────────────────
# create_sender_socket
# ────────────────────────────────────────────────────────────────────────────


@patch("multicast_video.socket_utils.socket.socket")
class TestCreateSenderSocket:
    def test_creates_udp_socket(self, MockSocket):
        create_sender_socket()
        MockSocket.assert_called_once_with(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )

    def test_sets_ttl(self, MockSocket):
        mock_sock = MockSocket.return_value
        create_sender_socket(ttl=5)
        mock_sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 5)

    def test_default_interface_does_not_set_multicast_if(self, MockSocket):
        mock_sock = MockSocket.return_value
        create_sender_socket(interface_ip="0.0.0.0")
        calls = [str(c) for c in mock_sock.setsockopt.call_args_list]
        assert not any("IP_MULTICAST_IF" in c for c in calls)

    def test_specific_interface_sets_multicast_if(self, MockSocket):
        mock_sock = MockSocket.return_value
        create_sender_socket(interface_ip="192.168.1.10")
        mock_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton("192.168.1.10"),
        )

    def test_returns_socket(self, MockSocket):
        sock = create_sender_socket()
        assert sock is MockSocket.return_value


# ────────────────────────────────────────────────────────────────────────────
# create_receiver_socket
# ────────────────────────────────────────────────────────────────────────────


@patch("multicast_video.socket_utils.socket.socket")
class TestCreateReceiverSocket:
    GROUP = "239.1.1.1"
    PORT = 5004

    def test_creates_udp_socket(self, MockSocket):
        create_receiver_socket(self.GROUP, self.PORT)
        MockSocket.assert_called_once_with(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )

    def test_sets_reuse_addr(self, MockSocket):
        mock_sock = MockSocket.return_value
        create_receiver_socket(self.GROUP, self.PORT)
        mock_sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def test_sets_so_rcvbuf(self, MockSocket):
        from multicast_video.config import UDP_RECV_BUFFER
        mock_sock = MockSocket.return_value
        create_receiver_socket(self.GROUP, self.PORT)
        mock_sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECV_BUFFER)

    def test_binds_to_inaddr_any(self, MockSocket):
        mock_sock = MockSocket.return_value
        create_receiver_socket(self.GROUP, self.PORT)
        mock_sock.bind.assert_called_once_with(("", self.PORT))

    def test_joins_group_default_interface(self, MockSocket):
        mock_sock = MockSocket.return_value
        create_receiver_socket(self.GROUP, self.PORT, interface_ip="0.0.0.0")
        expected_mreq = struct.pack("=4sl", socket.inet_aton(self.GROUP), socket.INADDR_ANY)
        mock_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, expected_mreq
        )

    def test_joins_group_specific_interface(self, MockSocket):
        mock_sock = MockSocket.return_value
        iface = "10.0.0.5"
        create_receiver_socket(self.GROUP, self.PORT, interface_ip=iface)
        expected_mreq = struct.pack(
            "=4s4s", socket.inet_aton(self.GROUP), socket.inet_aton(iface)
        )
        mock_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, expected_mreq
        )

    def test_returns_socket(self, MockSocket):
        sock = create_receiver_socket(self.GROUP, self.PORT)
        assert sock is MockSocket.return_value



# ────────────────────────────────────────────────────────────────────────────
# SO_REUSEPORT (platform-conditional branch)
# ────────────────────────────────────────────────────────────────────────────


def test_sets_reuseport_when_available():
    mock_sock = MagicMock()
    with patch("multicast_video.socket_utils.socket") as mock_module:
        mock_module.AF_INET = socket.AF_INET
        mock_module.SOCK_DGRAM = socket.SOCK_DGRAM
        mock_module.IPPROTO_UDP = socket.IPPROTO_UDP
        mock_module.SOL_SOCKET = socket.SOL_SOCKET
        mock_module.SO_REUSEADDR = socket.SO_REUSEADDR
        mock_module.SO_REUSEPORT = 15          # simulates Linux
        mock_module.IPPROTO_IP = socket.IPPROTO_IP
        mock_module.IP_ADD_MEMBERSHIP = socket.IP_ADD_MEMBERSHIP
        mock_module.INADDR_ANY = socket.INADDR_ANY
        mock_module.inet_aton = socket.inet_aton
        mock_module.socket.return_value = mock_sock
        create_receiver_socket("239.1.1.1", 5004)
    calls = [str(c) for c in mock_sock.setsockopt.call_args_list]
    assert any("15" in c for c in calls)


def test_reuseport_oserror_is_swallowed():
    mock_sock = MagicMock()

    def _setsockopt(level, opt, val):
        if opt == 15:
            raise OSError("not supported")

    mock_sock.setsockopt.side_effect = _setsockopt

    with patch("multicast_video.socket_utils.socket") as mock_module:
        mock_module.AF_INET = socket.AF_INET
        mock_module.SOCK_DGRAM = socket.SOCK_DGRAM
        mock_module.IPPROTO_UDP = socket.IPPROTO_UDP
        mock_module.SOL_SOCKET = socket.SOL_SOCKET
        mock_module.SO_REUSEADDR = socket.SO_REUSEADDR
        mock_module.SO_REUSEPORT = 15
        mock_module.IPPROTO_IP = socket.IPPROTO_IP
        mock_module.IP_ADD_MEMBERSHIP = socket.IP_ADD_MEMBERSHIP
        mock_module.INADDR_ANY = socket.INADDR_ANY
        mock_module.inet_aton = socket.inet_aton
        mock_module.socket.return_value = mock_sock
        create_receiver_socket("239.1.1.1", 5004)   # must not raise


# ────────────────────────────────────────────────────────────────────────────
# leave_multicast_group
# ────────────────────────────────────────────────────────────────────────────


class TestLeaveMulticastGroup:
    GROUP = "239.1.1.1"

    def test_drops_membership_default_interface(self):
        mock_sock = MagicMock()
        leave_multicast_group(mock_sock, self.GROUP)
        expected = struct.pack("=4sl", socket.inet_aton(self.GROUP), socket.INADDR_ANY)
        mock_sock.setsockopt.assert_called_once_with(
            socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, expected
        )

    def test_drops_membership_specific_interface(self):
        mock_sock = MagicMock()
        iface = "172.16.0.1"
        leave_multicast_group(mock_sock, self.GROUP, interface_ip=iface)
        expected = struct.pack("=4s4s", socket.inet_aton(self.GROUP), socket.inet_aton(iface))
        mock_sock.setsockopt.assert_called_once_with(
            socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, expected
        )

    def test_oserror_is_swallowed(self):
        mock_sock = MagicMock()
        mock_sock.setsockopt.side_effect = OSError("network error")
        # Should not raise.
        leave_multicast_group(mock_sock, self.GROUP)
