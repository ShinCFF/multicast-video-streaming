# Project Report: Multicast Video Streaming

## 1. Introduction

This project implements a real-time video streaming system over IP multicast using UDP.
The server continuously broadcasts MJPEG video frames to a multicast group; any number
of clients can join the group simultaneously and watch the stream without adding load to
the server — the network replicates packets at the router level.

## 2. System Design

### 2.1 Architecture

Two standalone programs communicate over UDP multicast:

| Component | Entry Point | Role |
|-----------|-------------|------|
| Server    | `Server.py` | Reads video file, encodes frames as JPEG, transmits over multicast |
| Client    | `Client.py` | Joins multicast group, reassembles frames, displays video + stats |

The underlying library (`src/multicast_video/`) is divided into:

- **`config.py`** – shared constants (group, port, FPS, buffer sizes)
- **`packet.py`** – wire format definition, encode/decode, chunking
- **`socket_utils.py`** – POSIX socket option helpers
- **`server.py`** – `MulticastVideoServer` class
- **`client.py`** – `MulticastVideoClient`, `_FrameBuffer`, `_LossTracker`

### 2.2 Custom Packet Format

Frames may exceed the UDP payload limit (~65 KB) and almost always exceed the
practical MTU (~1 400 bytes).  Each frame is therefore chunked:

```
┌────────┬─────────┬──────────┬─────────────┬──────────────┬──────────────┬─────────┐
│ magic  │ version │ frame_id │ chunk_index │ total_chunks │ timestamp_ms │ payload │
│  4 B   │   1 B   │   4 B    │     2 B     │     2 B      │     8 B      │  ≤1379B │
└────────┴─────────┴──────────┴─────────────┴──────────────┴──────────────┴─────────┘
                    Total header: 21 bytes
```

The `frame_id` is a 32-bit sequence number used by the receiver to detect packet loss.

### 2.3 Frame Reassembly

The receiver accumulates chunks in a `_FrameBuffer` keyed by `frame_id`.  Once
all `total_chunks` chunks for a frame have arrived, the payloads are concatenated
and decoded with `cv2.imdecode()`.  Buffers older than 2 seconds are evicted to
prevent unbounded memory growth in lossy networks.

### 2.4 Loss Detection

`_LossTracker` maintains:
- `_next_expected`: the frame_id expected to arrive next
- `_received`: number of complete frames decoded
- `_lost`: cumulative gap count (missed frame_ids)

When a frame with `frame_id > _next_expected` arrives, the difference is added
to `_lost`.  Loss rate (%) is overlaid on every displayed frame.

### 2.5 Multiple Clients

Because UDP multicast replicates at the network layer, any number of clients
can join `239.1.1.1:5004` independently.  The server is unaware of clients; it
simply sends to the group address.  Each client maintains its own reassembly
buffers and statistics independently.

## 3. Key Implementation Decisions

| Decision | Rationale |
|----------|-----------|
| JPEG frame encoding | Simple, widely supported; quality/size trade-off tunable (default 80%) |
| 1 400-byte max datagram | Fits comfortably within Ethernet MTU (1 500 B) minus IP+UDP headers |
| Bind to `""` (INADDR_ANY) | Compatible with both Linux and Windows; multicast filtering via `IP_ADD_MEMBERSHIP` |
| Separate receive/display threads | Prevents slow display operations from blocking the UDP receive buffer |
| Bounded frame queue (8 frames) | Limits memory usage; oldest frame dropped on overflow to stay live |
| 32-bit wrapping frame_id | Allows ~49 days of continuous streaming at 20 FPS before wrap |

## 4. Testing

Tests are located in `tests/` and run with `pytest`.

| Test Module          | Coverage |
|----------------------|----------|
| `test_packet.py`     | Encode/decode round-trip, error paths, chunking edge cases |
| `test_socket_utils.py` | Socket option calls (mocked) |
| `test_server.py`     | Lifecycle, frame sending, stream loop with EOF |
| `test_client.py`     | FrameBuffer, LossTracker, receive + reassembly (mocked socket) |

Run all tests:

```bash
pytest --cov=src/multicast_video --cov-report=term-missing
```

## 5. Running the System

### Prerequisites

```bash
pip install -r requirements.txt
```

### Server

```bash
python Server.py sample.mjpeg
```

### Client (run on same or different host on LAN)

```bash
python Client.py
```

Press **q** in the video window to exit.

## 6. Limitations and Future Work

- **No retransmission**: UDP is unreliable.  Lost chunks cause partial or skipped frames.  FEC or ARQ could improve quality on lossy links.
- **No flow control**: the server sends at a fixed FPS regardless of receiver capacity.
- **IPv4 only**: adding IPv6 multicast (`IPV6_JOIN_GROUP`) would require minor changes to `socket_utils.py`.
- **Single stream**: the current design supports one multicast group per server instance.
