#!/usr/bin/env python3
"""
Client entry point.

Usage:
    python Client.py [options]

Examples:
    python Client.py
    python Client.py --group 239.1.1.1 --port 5004
    python Client.py --interface 192.168.1.10 --timeout 30 -v
"""

import argparse
import logging
import sys

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from multicast_video.config import DEFAULT_INTERFACE, MULTICAST_GROUP, MULTICAST_PORT
from multicast_video.client import MulticastVideoClient


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Multicast MJPEG video client – joins a group and displays the stream.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--group", default=MULTICAST_GROUP, help="Multicast group IP address.")
    p.add_argument("--port", type=int, default=MULTICAST_PORT, help="UDP port.")
    p.add_argument("--interface", default=DEFAULT_INTERFACE, help="Local NIC IP address.")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="Seconds to wait for a packet before declaring stream ended.")
    p.add_argument("--window", default="Multicast Video", help="OpenCV window title.")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        with MulticastVideoClient(
            multicast_group=args.group,
            port=args.port,
            interface_ip=args.interface,
            recv_timeout_s=args.timeout,
        ) as client:
            client.display(window_title=args.window)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
