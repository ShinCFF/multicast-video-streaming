# Architecture

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        SERVER HOST                          │
│                                                             │
│  ┌──────────────┐   JPEG    ┌──────────────┐   UDP chunks  │
│  │ VideoCapture │ ────────▶ │ chunk_frame()│ ──────────┐   │
│  │  (cv2)       │           │  (packet.py) │           │   │
│  └──────────────┘           └──────────────┘           │   │
│         ▲                                               ▼   │
│   MJPEG file                                  ┌──────────┐  │
│                                               │ UDP sock │  │
│                                               └────┬─────┘  │
└────────────────────────────────────────────────────┼────────┘
                                                     │ multicast UDP
                                           239.1.1.1:5004
                    ┌────────────────────────────────┤
                    │                                │
         ┌──────────▼──────────┐          ┌──────────▼──────────┐
         │     CLIENT A        │          │     CLIENT B        │
         │                     │          │                     │
         │  ┌───────────────┐  │          │  ┌───────────────┐  │
         │  │  recv thread  │  │          │  │  recv thread  │  │
         │  │  UDP recvfrom │  │          │  │  UDP recvfrom │  │
         │  └──────┬────────┘  │          │  └──────┬────────┘  │
         │         │ queue     │          │         │ queue     │
         │  ┌──────▼────────┐  │          │  ┌──────▼────────┐  │
         │  │ display thread│  │          │  │ display thread│  │
         │  │  cv2.imshow   │  │          │  │  cv2.imshow   │  │
         │  └───────────────┘  │          │  └───────────────┘  │
         └─────────────────────┘          └─────────────────────┘
```

## Packet Format

Each video frame is encoded as JPEG and split into one or more UDP datagrams.
Every datagram carries a fixed 21-byte header:

```
Offset  Bytes  Field          Type    Notes
──────  ─────  ─────────────  ──────  ──────────────────────────────────
     0      4  magic          bytes   b"MVSF" – fast sanity check
     4      1  version        uint8   Protocol version = 1
     5      4  frame_id       uint32  Monotonically increasing; wraps at 2³²
     9      2  chunk_index    uint16  0-based chunk position within the frame
    11      2  total_chunks   uint16  Number of chunks this frame was split into
    13      8  timestamp_ms   uint64  Sender wall-clock (ms since Unix epoch)
    21      N  payload        bytes   JPEG slice; N ≤ 1379 bytes
```

**Maximum datagram size**: 21 (header) + 1379 (payload) = 1400 bytes, safely
below the 1500-byte Ethernet MTU with room for IP + UDP headers (~28 bytes).

**Loss detection**: The receiver watches `frame_id` for gaps.  If `frame_id`
jumps by more than 1, the difference is counted as lost frames.

## Component Responsibilities

### `config.py`
All tuneable constants in one place.  No magic numbers elsewhere.

### `packet.py`
Pure data-serialisation layer:
- `VideoPacket` dataclass with `encode()` / `decode()` methods.
- `chunk_frame()` splits an arbitrary-length JPEG byte string into correctly
  sized `VideoPacket` objects ready for `sendto()`.

### `socket_utils.py`
Thin wrappers around `socket.setsockopt()` calls for multicast sender and
receiver creation.  Also handles `IP_DROP_MEMBERSHIP` on exit.

### `server.py` / `MulticastVideoServer`
- Opens a `cv2.VideoCapture` and a sender socket.
- Reads frames in a loop, JPEG-encodes them, calls `chunk_frame()`, and
  sends all resulting datagrams to the multicast endpoint.
- Loops back to the first frame on EOF.
- Paces output to the configured FPS using `time.monotonic()` sleep.

### `client.py` / `MulticastVideoClient`
- Joins the multicast group on a receiver socket.
- **Receive thread**: calls `recvfrom()` in a tight loop, parses packets,
  accumulates chunks in `_FrameBuffer` objects keyed by `frame_id`, and
  pushes complete frames onto a bounded `queue.Queue`.
- **Display thread (main)**: dequeues frames, overlays loss statistics, and
  calls `cv2.imshow()`.
- `_LossTracker` computes running received / lost / loss-rate statistics.

## Concurrency

```
Main thread                  Receive thread (daemon)
──────────                   ──────────────────────
display_loop()               receive_loop()
  ├─ queue.get(timeout)  ◀──── queue.put_nowait(frame)
  ├─ overlay_stats()           ├─ recvfrom()
  └─ cv2.imshow()             └─ _process_datagram()
```

The two threads share only a thread-safe `queue.Queue`.  The receive thread
is a daemon so it exits automatically when the main thread returns.

## Network Layer

- **Protocol**: UDP (connectionless, no retransmission)
- **Multicast group**: `239.1.1.1` (administratively scoped, RFC 2365)
- **Port**: `5004` (matches RTP convention; no RTP framing used)
- **TTL**: `1` (default, LAN only; increase to cross subnets)
- **Membership**: `IP_ADD_MEMBERSHIP` / `IP_DROP_MEMBERSHIP` via IGMP
