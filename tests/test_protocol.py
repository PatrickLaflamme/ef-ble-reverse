"""Wire-protocol tests: Packet framing/CRC and EncPacket encrypt/decrypt.

These are pure and deterministic — a regression here corrupts every command.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connect  # noqa: E402
from _common import REAL_KEY, REAL_IV, REAL_FRAME, make_conn  # noqa: E402


class PacketTests(unittest.TestCase):
    def test_roundtrip_fields(self):
        p = connect.Packet(0x21, 0x02, 0x02, 0x6A, b"\x08\x00",
                           1, 1, 0x13, seq=b"\x00\x00\x00\x00")
        q = connect.Packet.fromBytes(p.toBytes(), is_xor=True)  # seq[0]==0 -> no xor
        self.assertIsNotNone(q)
        self.assertEqual(q.src, 0x21)
        self.assertEqual(q.dst, 0x02)
        self.assertEqual(q.cmdSet, 0x02)
        self.assertEqual(q.cmdId, 0x6A)
        self.assertEqual(q.payload, b"\x08\x00")

    def test_empty_payload_roundtrip(self):
        p = connect.Packet(0x21, 0x02, 0x02, 0x01, b"", version=0x13)
        q = connect.Packet.fromBytes(p.toBytes(), is_xor=True)
        self.assertIsNotNone(q)
        self.assertEqual(q.payload, b"")

    def test_header_crc8_rejects_corruption(self):
        raw = bytearray(connect.Packet(0x21, 0x02, 0x02, 0x44, b"\x08\x01",
                                       version=0x13).toBytes())
        raw[4] ^= 0xFF  # corrupt the header CRC8 byte
        self.assertIsNone(connect.Packet.fromBytes(bytes(raw)))

    def test_too_short_rejected(self):
        self.assertIsNone(connect.Packet.fromBytes(b"\xaa\x13\x00"))

    def test_bad_prefix_rejected(self):
        raw = bytearray(connect.Packet(0x21, 0x02, 0x02, 0x44, b"\x01",
                                       version=0x13).toBytes())
        raw[0] = 0x00
        self.assertIsNone(connect.Packet.fromBytes(bytes(raw)))

    def test_parses_real_captured_frame(self):
        # Real Y711 frame from the journal (version 0x13, XOR with seq[0]).
        p = connect.Packet.fromBytes(REAL_FRAME, is_xor=True)
        self.assertIsNotNone(p)
        self.assertEqual(REAL_FRAME[:1], connect.Packet.PREFIX)


class EncPacketTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypt_then_parse_roundtrip(self):
        inner = connect.Packet(0x21, 0x02, 0x02, 0x6A, b"\x08\x00",
                              version=0x13, seq=b"\x00\x00\x00\x00")
        frame = connect.EncPacket(
            connect.EncPacket.FRAME_TYPE_PROTOCOL,
            connect.EncPacket.PAYLOAD_TYPE_VX_PROTOCOL,
            inner.toBytes(), 0, 0, REAL_KEY, REAL_IV,
        ).toBytes()
        self.assertTrue(frame.startswith(connect.EncPacket.PREFIX))

        conn = make_conn(dev_sn="Y711TEST")
        packets = await conn.parseEncPackets(frame)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].cmdSet, 0x02)
        self.assertEqual(packets[0].cmdId, 0x6A)
        self.assertEqual(packets[0].payload, b"\x08\x00")

    async def test_unencrypted_passthrough(self):
        # No key/iv => payload not encrypted.
        ep = connect.EncPacket(connect.EncPacket.FRAME_TYPE_PROTOCOL,
                              connect.EncPacket.PAYLOAD_TYPE_VX_PROTOCOL, b"hello")
        self.assertEqual(ep.encryptPayload(), b"hello")


if __name__ == "__main__":
    unittest.main()
