"""
Tests for multicast_video.packet – wire format encode/decode and chunking.
"""

import struct
import time

import pytest

from multicast_video.packet import (
    HEADER_SIZE,
    MAGIC,
    MAX_CHUNK_PAYLOAD,
    VERSION,
    VideoPacket,
    chunk_frame,
)


# ────────────────────────────────────────────────────────────────────────────
# VideoPacket.encode / decode round-trip
# ────────────────────────────────────────────────────────────────────────────


class TestVideoPacketRoundTrip:
    def test_encode_produces_bytes(self, sample_packet):
        wire = sample_packet.encode()
        assert isinstance(wire, bytes)

    def test_encoded_length(self, sample_packet):
        wire = sample_packet.encode()
        assert len(wire) == HEADER_SIZE + len(sample_packet.payload)

    def test_decode_restores_all_fields(self, sample_packet):
        decoded = VideoPacket.decode(sample_packet.encode())
        assert decoded.frame_id == sample_packet.frame_id
        assert decoded.chunk_index == sample_packet.chunk_index
        assert decoded.total_chunks == sample_packet.total_chunks
        assert decoded.timestamp_ms == sample_packet.timestamp_ms
        assert decoded.payload == sample_packet.payload

    def test_round_trip_empty_payload(self):
        pkt = VideoPacket(frame_id=0, chunk_index=0, total_chunks=1, timestamp_ms=0, payload=b"")
        assert VideoPacket.decode(pkt.encode()).payload == b""

    def test_round_trip_large_payload(self):
        payload = bytes(range(256)) * 5
        pkt = VideoPacket(frame_id=99, chunk_index=3, total_chunks=10, timestamp_ms=999, payload=payload)
        assert VideoPacket.decode(pkt.encode()).payload == payload

    def test_magic_in_wire_bytes(self, sample_packet):
        wire = sample_packet.encode()
        assert wire[:4] == MAGIC

    def test_version_in_wire_bytes(self, sample_packet):
        wire = sample_packet.encode()
        assert wire[4] == VERSION

    def test_max_frame_id(self):
        pkt = VideoPacket(frame_id=0xFFFF_FFFF, chunk_index=0, total_chunks=1, timestamp_ms=0, payload=b"x")
        assert VideoPacket.decode(pkt.encode()).frame_id == 0xFFFF_FFFF


# ────────────────────────────────────────────────────────────────────────────
# VideoPacket.decode – error cases
# ────────────────────────────────────────────────────────────────────────────


class TestVideoPacketDecodeErrors:
    def test_raises_on_truncated_data(self):
        with pytest.raises(ValueError, match="too short"):
            VideoPacket.decode(b"\x00" * (HEADER_SIZE - 1))

    def test_raises_on_bad_magic(self, sample_packet):
        wire = bytearray(sample_packet.encode())
        wire[:4] = b"XXXX"
        with pytest.raises(ValueError, match="magic"):
            VideoPacket.decode(bytes(wire))

    def test_raises_on_unsupported_version(self, sample_packet):
        wire = bytearray(sample_packet.encode())
        wire[4] = 99  # unsupported version
        with pytest.raises(ValueError, match="version"):
            VideoPacket.decode(bytes(wire))

    def test_empty_bytes_raises(self):
        with pytest.raises(ValueError):
            VideoPacket.decode(b"")

    def test_raises_on_chunk_index_out_of_range(self, sample_packet):
        """chunk_index >= total_chunks must be rejected (fix #4)."""
        wire = bytearray(sample_packet.encode())
        # Overwrite chunk_index (offset 9, uint16 big-endian) to 1,
        # while total_chunks (offset 11) stays at 1 → index 1 >= total 1.
        wire[9] = 0
        wire[10] = 1   # chunk_index = 1
        wire[11] = 0
        wire[12] = 1   # total_chunks = 1
        with pytest.raises(ValueError, match="out of range"):
            VideoPacket.decode(bytes(wire))

    def test_raises_on_zero_total_chunks(self, sample_packet):
        """total_chunks = 0 is nonsensical and must be rejected."""
        wire = bytearray(sample_packet.encode())
        wire[11] = 0
        wire[12] = 0   # total_chunks = 0
        with pytest.raises(ValueError, match="total_chunks"):
            VideoPacket.decode(bytes(wire))


# ────────────────────────────────────────────────────────────────────────────
# chunk_frame
# ────────────────────────────────────────────────────────────────────────────


class TestChunkFrame:
    def test_small_data_fits_in_one_chunk(self):
        data = b"A" * 100
        packets = chunk_frame(data, frame_id=1)
        assert len(packets) == 1
        assert packets[0].chunk_index == 0
        assert packets[0].total_chunks == 1
        assert packets[0].payload == data

    def test_large_data_splits_into_multiple_chunks(self, large_jpeg):
        packets = chunk_frame(large_jpeg, frame_id=2)
        assert len(packets) > 1
        assert packets[0].total_chunks == len(packets)

    def test_all_chunks_share_same_frame_id(self, large_jpeg):
        packets = chunk_frame(large_jpeg, frame_id=7)
        assert all(p.frame_id == 7 for p in packets)

    def test_chunk_indices_are_sequential(self, large_jpeg):
        packets = chunk_frame(large_jpeg, frame_id=0)
        assert [p.chunk_index for p in packets] == list(range(len(packets)))

    def test_reassembly_restores_original_data(self, large_jpeg):
        packets = chunk_frame(large_jpeg, frame_id=0)
        reassembled = b"".join(p.payload for p in sorted(packets, key=lambda p: p.chunk_index))
        assert reassembled == large_jpeg

    def test_exact_max_chunk_size_is_one_chunk(self):
        data = b"B" * MAX_CHUNK_PAYLOAD
        packets = chunk_frame(data, frame_id=0)
        assert len(packets) == 1

    def test_one_byte_over_max_splits(self):
        data = b"C" * (MAX_CHUNK_PAYLOAD + 1)
        packets = chunk_frame(data, frame_id=0)
        assert len(packets) == 2

    def test_each_encoded_packet_fits_within_max_udp_payload(self, large_jpeg):
        from multicast_video.config import MAX_UDP_PAYLOAD
        for pkt in chunk_frame(large_jpeg, frame_id=0):
            assert len(pkt.encode()) <= MAX_UDP_PAYLOAD

    def test_empty_data_returns_single_packet(self):
        packets = chunk_frame(b"", frame_id=0)
        assert len(packets) == 1
        assert packets[0].payload == b""

    def test_explicit_timestamp_is_used(self):
        ts = 1_234_567_890_000
        packets = chunk_frame(b"hello", frame_id=0, timestamp_ms=ts)
        assert packets[0].timestamp_ms == ts

    def test_default_timestamp_is_recent(self):
        before = int(time.time() * 1000)
        packets = chunk_frame(b"hello", frame_id=0)
        after = int(time.time() * 1000)
        assert before <= packets[0].timestamp_ms <= after

    def test_raises_when_total_chunks_exceeds_uint16(self):
        """Frames requiring > 65535 chunks must raise ValueError (fix #5)."""
        from multicast_video.packet import MAX_CHUNK_PAYLOAD, _MAX_TOTAL_CHUNKS
        # Build data exactly one byte over the uint16 limit.
        huge = b"X" * (MAX_CHUNK_PAYLOAD * _MAX_TOTAL_CHUNKS + 1)
        with pytest.raises(ValueError, match="too large"):
            chunk_frame(huge, frame_id=0)
