#!/usr/bin/env python3
"""
Server entry point.

Usage:
    python Server.py <MJPEG_video_file> [options]

Examples:
    python Server.py sample.mjpeg
    python Server.py sample.avi --fps 25 --quality 75
    python Server.py sample.mp4 --group 239.1.1.1 --port 5004
"""

import argparse
import logging
import sys

# Allow running directly without installing the package.
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from multicast_video.config import MULTICAST_GROUP, MULTICAST_PORT, TARGET_FPS, JPEG_QUALITY, TTL
from multicast_video.server import MulticastVideoServer


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Multicast MJPEG video server – broadcasts a video file to a multicast group.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("video", help="Path to the MJPEG / video file to stream.")
    p.add_argument("--group", default=MULTICAST_GROUP, help="Multicast group IP address.")
    p.add_argument("--port", type=int, default=MULTICAST_PORT, help="UDP port.")
    p.add_argument("--ttl", type=int, default=TTL, help="Multicast TTL (hop limit).")
    p.add_argument("--interface", default="0.0.0.0", help="Source NIC IP address.")
    p.add_argument("--fps", type=float, default=TARGET_FPS, help="Target frames per second.")
    p.add_argument("--quality", type=int, default=JPEG_QUALITY, dest="jpeg_quality",
                   help="JPEG encode quality (0-100).")
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
        with MulticastVideoServer(
            video_path=args.video,
            multicast_group=args.group,
            port=args.port,
            ttl=args.ttl,
            interface_ip=args.interface,
            fps=args.fps,
            jpeg_quality=args.jpeg_quality,
        ) as srv:
            srv.stream()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
