"""Shared helpers and real captured fixtures for the test suite.

Not a test module (doesn't match test_*). Imported by the test files.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connect  # noqa: E402
from Crypto.Cipher import AES  # noqa: E402
from Crypto.Util.Padding import unpad  # noqa: E402

# Real session key/iv captured via Wireshark (from parsePacketsJson.py).
REAL_KEY = bytes.fromhex("2fc6e4ed3bb6400993cfb95555147722")
REAL_IV = bytes.fromhex("9dd18ee65e40440552e3787f7f4b6a2e")

# A real *decrypted* Packet frame captured from a live device (journal dump).
# version 0x13, Y711 (XOR with seq[0]); used to prove we parse real wire bytes.
REAL_FRAME = bytes.fromhex(
    "aa1352005a2c635b0200011d0221010102036b6373637b5143634b7d53625b5f23622b39"
    "33693b6303690beb6d13eb6d1b1be362cf61eb62b366f362b366fb6263c36263cb62fc65"
    "d16273220e06110a00024c2d06143c3a0c1108db6263a36263ab6263a7d8"
)


class DummyDev:
    def __init__(self, address="AA:BB:CC:DD:EE:01", name="DPU"):
        self.address = address
        self.name = name


def make_conn(dev_sn="Y711TEST", key=REAL_KEY, iv=REAL_IV):
    """A Connection with an injected session key (skips the ECDH handshake)."""
    c = connect.Connection(DummyDev(), dev_sn)
    c._session_key = key
    c._iv = iv
    return c


def enc_packet(inner_packet, key=REAL_KEY, iv=REAL_IV):
    """Wrap a Packet in an encrypted EncPacket frame (what goes over BLE)."""
    return connect.EncPacket(
        connect.EncPacket.FRAME_TYPE_PROTOCOL,
        connect.EncPacket.PAYLOAD_TYPE_VX_PROTOCOL,
        inner_packet.toBytes(), 0, 0, key, iv,
    ).toBytes()


def decode_enc(frame, key=REAL_KEY, iv=REAL_IV, is_xor=False):
    """Decrypt one EncPacket frame back to a Packet (mirrors parseEncPackets)."""
    payload_enc = frame[6:-2]
    inner = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(payload_enc), 16)
    return connect.Packet.fromBytes(inner, is_xor)


class FakeBleakClient:
    """Minimal stand-in for bleak's client: records GATT writes."""

    def __init__(self):
        self.is_connected = True
        self.writes = []

    async def write_gatt_char(self, char, data, *a, **k):
        self.writes.append(bytes(data))

    async def start_notify(self, *a, **k):
        pass

    async def stop_notify(self, *a, **k):
        pass

    async def disconnect(self):
        self.is_connected = False
