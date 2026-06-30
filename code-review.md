## Code Review

All 77 tests pass and the architecture is clean. The issues below are grouped by severity.

---

### Bug — `_LossTracker.record()`: out-of-order frames corrupt `_next_expected`

client.py

```python
self._next_expected = (frame_id + 1) & 0xFFFF_FFFF   # ← always overwrites
```

If a late/reordered frame arrives with `frame_id < _next_expected`, this moves `_next_expected` **backward**, causing future in-order frames to be falsely counted as losses. Example:

- Receive 0 → `_next_expected = 1`
- Receive 5 → lost += 4, `_next_expected = 6`
- Late packet frame 3 arrives → gap is negative, no loss (correct), but `_next_expected` is reset to **4**
- Receive 7 → gap = 3, lost += 3 ← double-counts 4, 5, 6

Fix: only advance `_next_expected` when moving forward.

---

### Bug — `_LossTracker.record()`: 32-bit rollover is not correctly detected

Python integers do not wrap, so when `frame_id` wraps from `0xFFFF_FFFF` to `0` and `_next_expected = 0xFFFF_FFFE`, the gap is computed as `0 - 0xFFFF_FFFE = -4294967294` — treated as a late packet, and `_next_expected` is reset to `1`. The wrap transition is silently mis-handled. The CLAUDE.md claims this is handled, but it isn't.

Fix: use signed modular comparison: `gap = (frame_id - _next_expected) & 0xFFFF_FFFF`; if `gap > 0x8000_0000` it's a backward jump (late/duplicate), otherwise it's a forward gap.

---

### Issue — Receiver socket receive buffer is never configured

socket_utils.py / config.py

`UDP_RECV_BUFFER = 65536` is defined but only passed as the `bufsize` argument to `recvfrom()` — that is the *application* read buffer, not the OS socket buffer. `SO_RCVBUF` is never set, so on Windows (default 64 KB) bursts of video chunks at 20 FPS can overflow the OS buffer and cause packet loss before the application even reads them.

Fix: add `sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECV_BUFFER)` in `create_receiver_socket()`.

---

### Issue — `VideoPacket.decode()`: no validation that `chunk_index < total_chunks`

packet.py

A malformed or adversarial packet with `chunk_index >= total_chunks` will be stored in `_FrameBuffer.chunks` at an out-of-range key. Since `is_complete` checks `len(chunks) == total_chunks`, the spurious entry means the real chunks 0…N-1 can all arrive and the frame will still never complete — it hangs until the 2-second eviction timeout. At high volume this would silently drop every frame with that `frame_id`.

---

### Issue — `chunk_frame()`: no guard against `total_chunks` exceeding `uint16` max

packet.py

`total_chunks` is packed as a `uint16` (max 65 535). A JPEG larger than `65535 × 1379 ≈ 90 MB` would silently overflow, producing packets with wrong `total_chunks` values. A `ValueError` with a helpful message would be safer than a silent `struct.error` at pack-time.

---

### Issue — Server `stream()`: EOF path does not reset `t_start`, causing timing drift

server.py

```python
if not ret:
    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    continue          # ← t_start is stale
```

On the very next iteration, `t_start` is still the timestamp of the previous read, so `elapsed = time.monotonic() - t_start` may be large and `sleep_for` will be negative — the following frame is sent immediately, causing a micro-burst at the loop point.

---

### Issue — `_evict_stale_buffers()` called on every new `frame_id`

client.py

`_evict_stale_buffers()` iterates over all pending buffers every time a packet with a new `frame_id` arrives (20+ times per second). At steady state this is cheap, but in burst or congested scenarios with many incomplete buffers it becomes a linear scan per packet. A simple timestamp-gated approach (e.g., only evict if >0.5 s have elapsed since last eviction) would be more efficient.

---

### Minor — Drop-oldest-frame logic could raise `queue.Full` in theory

client.py

```python
except queue.Full:
    try:
        self._frame_queue.get_nowait()
    except queue.Empty:
        pass
    self._frame_queue.put_nowait(frame)   # ← unsafe if get also failed
```

If `get_nowait()` raises `queue.Empty` (display thread drained the queue between the two calls), the second `put_nowait` is safe because the queue is now empty. But the intent is ambiguous and a `queue.put(frame, block=False)` inside a `try/except Full` after the get would be clearer.

---

### Minor — `typing.List` / `typing.Optional` instead of built-in generics

packet.py

These are from the `typing` module which is deprecated for this use since Python 3.9. `list[VideoPacket]` and `int | None` are idiomatic for modern Python.

---

### Summary table

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| 1 | **Bug** | `_LossTracker.record()` | Out-of-order frame resets `_next_expected` backward |
| 2 | **Bug** | `_LossTracker.record()` | 32-bit wrap-around not correctly computed (Python int subtraction) |
| 3 | Medium | `create_receiver_socket()` | `SO_RCVBUF` never set; `UDP_RECV_BUFFER` only controls `recvfrom()` buffer |
| 4 | Medium | `VideoPacket.decode()` | No assertion `chunk_index < total_chunks` |
| 5 | Medium | `chunk_frame()` | No guard for `total_chunks > 65535` overflow |
| 6 | Low | `server.stream()` | Stale `t_start` after EOF loop causes timing drift |
| 7 | Low | `_evict_stale_buffers()` | Called on every new frame_id; should be rate-limited |
| 8 | Low | `_receive_loop` | Drop-oldest logic is correct but misleadingly structured |
| 9 | Style | packet.py | `typing.List/Optional` → use built-in types |