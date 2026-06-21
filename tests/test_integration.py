"""Integration tests across the connect <-> controller seam, no hardware.

Uses a real Connection with an injected session key, real protobuf heartbeats,
real AES/CRC framing, and a FakeBleakClient transport. Drives the full path:
encrypted notify in -> decrypt/parse -> controller decision -> encrypted
command out -> decrypt and verify the bytes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connect  # noqa: E402
import controller  # noqa: E402
import policy  # noqa: E402
import yj751_sys_pb2_v4 as pb  # noqa: E402
from _common import (REAL_KEY, REAL_IV, make_conn, enc_packet, decode_enc,  # noqa: E402
                     FakeBleakClient)


def heartbeat_frame(soc, a5p8, show_flag=132):
    """Build an encrypted AppShowHeartbeatReport frame as the device would send."""
    msg = pb.AppShowHeartbeatReport(soc=soc, access_5p8_out_type=a5p8,
                                    show_flag=show_flag)
    pkt = connect.Packet(0x02, 0x21, 0x02, 0x01, msg.SerializeToString(),
                        1, 1, 0x13, seq=b"\x00\x00\x00\x00")
    return enc_packet(pkt)


class HeartbeatParseTests(unittest.IsolatedAsyncioTestCase):
    """#9 — a real Connection decrypts a heartbeat and exposes state + replies."""

    def setUp(self):
        self._orig_cmd = connect.CMD
        connect.CMD = None  # don't fire the one-shot CLI command

    def tearDown(self):
        connect.CMD = self._orig_cmd

    async def test_heartbeat_sets_state_fires_callback_and_replies(self):
        conn = make_conn(dev_sn="Y711UNITA")
        conn._client = FakeBleakClient()
        fired = []

        async def cb(c):  # heartbeat callback contract is async
            fired.append(c.last_soc)

        conn.set_heartbeat_callback(cb)

        await conn.listenForDataHandler(None, heartbeat_frame(soc=78, a5p8=1, show_flag=132))

        self.assertEqual(conn.last_soc, 78)
        self.assertEqual(conn._last_access_5p8_out_type, 1)
        self.assertEqual(conn._last_show_flag, 132)
        self.assertEqual(fired, [78])           # callback fired with parsed soc
        self.assertTrue(conn._client.writes)    # replied to the device
        reply = decode_enc(conn._client.writes[0])
        self.assertEqual(reply.cmdId, 0x01)     # echo of the heartbeat


class MockBleEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """#10 — full stack: encrypted heartbeats in -> controller -> command out."""

    def setUp(self):
        controller.MAKE_SETTLE_SECONDS = 0
        self._orig_cmd = connect.CMD
        self._orig_win = policy.in_window
        connect.CMD = None
        policy.in_window = lambda now=None: False

    def tearDown(self):
        connect.CMD = self._orig_cmd
        policy.in_window = self._orig_win

    async def test_low_unit_gets_detach_command(self):
        conn_a = make_conn(dev_sn="Y711UNITA")
        conn_b = make_conn(dev_sn="Y711UNITB")
        conn_a._client = FakeBleakClient()
        conn_b._client = FakeBleakClient()

        ctrl = controller.ParallelController()
        ctrl.add(conn_a.sn, conn_a)
        ctrl.add(conn_b.sn, conn_b)

        # A is at the floor (38), B healthy (70); both currently attached (a5p8=1).
        await conn_a.listenForDataHandler(None, heartbeat_frame(38, 1))  # incomplete
        await conn_b.listenForDataHandler(None, heartbeat_frame(70, 1))  # triggers decision

        # Controller should have decided to charge A.
        self.assertEqual(ctrl._charging, conn_a.sn)

        # Decode everything A was sent; find the PrStateSet detach command.
        decoded = [decode_enc(w) for w in conn_a._client.writes]
        pbox = [p for p in decoded if p and p.cmdId == 0x6A]
        self.assertEqual(len(pbox), 1)
        self.assertEqual(pbox[0].cmdSet, 0x02)
        self.assertEqual(pbox[0].payload, b"\x08\x00")  # PrStateSet set_self=0

        # B (load-bearing) should NOT have received a detach.
        decoded_b = [decode_enc(w) for w in conn_b._client.writes]
        self.assertEqual([p for p in decoded_b if p and p.cmdId == 0x6A], [])


if __name__ == "__main__":
    unittest.main()
