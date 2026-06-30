# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

UDP multicast MJPEG video streaming in Python.  
**Server**: `python Server.py <video_file>` — reads MJPEG, streams to `239.1.1.1:5004` at 20 FPS, loops on EOF.  
**Client**: `python Client.py` — joins multicast group, reassembles frames, displays with loss statistics.

Requirements document: `Multicast_Video_Streaming_Project_Requirement.pdf`.

## Commands

```bash
# Install runtime deps
pip install -r requirements.txt

# Install dev deps (includes pytest)
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run with coverage
pytest --cov=src/multicast_video --cov-report=term-missing

# Run a single test file
pytest tests/test_packet.py -v
```

## Project Layout

```
src/multicast_video/   # importable library (added to sys.path by Server.py / Client.py)
  config.py            # constants: MULTICAST_GROUP="239.1.1.1", PORT=5004, TARGET_FPS=20
  packet.py            # VideoPacket dataclass + chunk_frame(); 21-byte header (big-endian)
  socket_utils.py      # create_sender_socket(), create_receiver_socket(), leave_multicast_group()
  server.py            # MulticastVideoServer – open/close/stream loop
  client.py            # MulticastVideoClient – recv thread + display thread + _LossTracker
Server.py              # CLI entry point for server (argparse wrapper)
Client.py              # CLI entry point for client (argparse wrapper)
tests/                 # pytest suite; all socket I/O is mocked (no network needed)
docs/                  # architecture.md (packet format + diagrams), report.md
```

## Packet Format (21-byte header, network byte order)

| Offset | Size | Field         | Notes                            |
|--------|------|---------------|----------------------------------|
| 0      | 4    | magic         | `b"MVSF"`                       |
| 4      | 1    | version       | `1`                              |
| 5      | 4    | frame_id      | uint32, wraps at 2³²             |
| 9      | 2    | chunk_index   | 0-based within this frame        |
| 11     | 2    | total_chunks  | total chunks for this frame      |
| 13     | 8    | timestamp_ms  | sender wall-clock (ms)           |
| 21     | ≤1379| payload      | JPEG slice; MAX_UDP_PAYLOAD=1400 |

## Key Implementation Details

- **Receiver binds to `("", port)`** (INADDR_ANY) — required on Windows; Linux accepts either.
- **`IP_ADD_MEMBERSHIP` struct**: `struct.pack("=4sl", inet_aton(group), INADDR_ANY)` for default NIC.
- **Two threads on client**: receive thread fills a bounded `queue.Queue(8)`; display thread reads it. Prevents `imshow()` blocking `recvfrom()`.
- **Frame buffers**: keyed by `frame_id`, evicted after `FRAME_BUFFER_TIMEOUT_S=2.0` seconds if incomplete.
- **Loss detection**: gap in `frame_id` = lost frames; displayed as overlay + logged every 5 seconds.
- **EOF handling**: server calls `cap.set(cv2.CAP_PROP_POS_FRAMES, 0)` to loop.
- **Frame ID wraps**: `self._frame_id = (self._frame_id + 1) & 0xFFFF_FFFF`; `_LossTracker` handles 32-bit rollover.

## Testing Approach

Tests mock the network layer (`create_sender_socket`, `create_receiver_socket`, `socket.recvfrom`) so no real multicast interface is needed.  OpenCV is used in fixtures to create minimal JPEG frames.  All core logic (encode/decode, chunking, reassembly, loss tracking, server loop) is tested.
