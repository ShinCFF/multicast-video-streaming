"""
Low-level UDP multicast socket factory functions.

Design notes
────────────
• Sender socket: plain UDP socket; sets IP_MULTICAST_TTL so datagrams reach
  the right network scope, and IP_MULTICAST_IF to pin the outgoing NIC.

• Receiver socket: enables SO_REUSEADDR (+ SO_REUSEPORT where available) so
  multiple processes on the same host can each join the same multicast group.
  Binds to INADDR_ANY (portable across Linux and Windows) then joins the
  multicast group on the requested interface via IP_ADD_MEMBERSHIP.
"""

import socket
import struct
import logging
import sys

logger = logging.getLogger(__name__)


def create_sender_socket(interface_ip: str = "0.0.0.0", ttl: int = 1) -> socket.socket:
    """Create and configure a UDP socket for sending multicast datagrams.

    Args:
        interface_ip: IP address of the local NIC to use for outgoing datagrams.
                      ``"0.0.0.0"`` lets the OS pick the default interface.
        ttl:          Multicast TTL (hop limit).  1 = LAN only.

    Returns:
        A configured, unbound :class:`socket.socket`.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    if interface_ip and interface_ip != "0.0.0.0":
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(interface_ip),
        )
    logger.debug("Sender socket created (ttl=%d, iface=%s)", ttl, interface_ip)
    return sock


def create_receiver_socket(
    multicast_group: str,
    port: int,
    interface_ip: str = "0.0.0.0",
) -> socket.socket:
    """Create and configure a UDP socket for receiving multicast datagrams.

    The socket is bound to INADDR_ANY so it is compatible with both Linux and
    Windows.  Multicast group membership is established with IP_ADD_MEMBERSHIP.

    Args:
        multicast_group: Multicast group address to join (e.g. ``"239.1.1.1"``).
        port:            UDP port to bind.
        interface_ip:    Local NIC to receive on.  ``"0.0.0.0"`` = default NIC.

    Returns:
        A configured, bound, and group-joined :class:`socket.socket`.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Enlarge the OS receive buffer so burst traffic does not overflow before
    # the application calls recvfrom().  UDP_RECV_BUFFER controls both this
    # kernel-side buffer and the per-call read size.
    from .config import UDP_RECV_BUFFER
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECV_BUFFER)
    except OSError:
        pass  # best-effort; some platforms cap SO_RCVBUF at a system limit

    # SO_REUSEPORT lets multiple processes receive on the same (group, port);
    # not available on Windows, optional on Linux.
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    # Bind to INADDR_ANY for cross-platform compatibility.
    sock.bind(("", port))

    # Join the multicast group on the requested interface.
    if interface_ip == "0.0.0.0":
        mreq = struct.pack("=4sl", socket.inet_aton(multicast_group), socket.INADDR_ANY)
    else:
        mreq = struct.pack(
            "=4s4s",
            socket.inet_aton(multicast_group),
            socket.inet_aton(interface_ip),
        )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    logger.info(
        "Receiver socket joined %s:%d on interface %s",
        multicast_group,
        port,
        interface_ip,
    )
    return sock


def leave_multicast_group(
    sock: socket.socket,
    multicast_group: str,
    interface_ip: str = "0.0.0.0",
) -> None:
    """Send IGMP leave for the multicast group before closing the socket."""
    try:
        if interface_ip == "0.0.0.0":
            mreq = struct.pack("=4sl", socket.inet_aton(multicast_group), socket.INADDR_ANY)
        else:
            mreq = struct.pack(
                "=4s4s",
                socket.inet_aton(multicast_group),
                socket.inet_aton(interface_ip),
            )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
        logger.info("Left multicast group %s", multicast_group)
    except OSError as exc:
        logger.warning("Failed to drop multicast membership: %s", exc)
