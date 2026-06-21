"""Command-payload correctness: the safety-critical seam.

Asserts each setter emits the exact cmd_set/cmd_id/payload bytes — a regression
here would silently send the wrong command to a 1.5 kW power system.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connect  # noqa: E402
import yj751_sys_pb2_v4 as pb  # noqa: E402
from _common import make_conn  # noqa: E402


def capture(conn):
    """Replace sendPacket with a recorder; returns the list it appends to."""
    sent = []

    async def rec(packet, response_handler=None):
        sent.append(packet)

    conn.sendPacket = rec
    return sent


class CommandPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_parallel_box_detach(self):
        conn = make_conn()
        sent = capture(conn)
        await conn.setParallelBox(set_self=0)
        self.assertEqual(len(sent), 1)
        p = sent[0]
        self.assertEqual((p.src, p.dst), (0x21, 0x02))
        self.assertEqual((p.cmdSet, p.cmdId), (0x02, 0x6A))
        self.assertEqual(p.payload, b"\x08\x00")  # PrStateSet set_self=0

    async def test_set_parallel_box_attach(self):
        conn = make_conn()
        sent = capture(conn)
        await conn.setParallelBox(set_self=1)
        self.assertEqual(sent[0].payload, b"\x08\x01")
        self.assertEqual((sent[0].cmdSet, sent[0].cmdId), (0x02, 0x6A))

    async def test_set_parallel_box_para_only(self):
        conn = make_conn()
        sent = capture(conn)
        await conn.setParallelBox(set_para=0)
        # only field 2 serialized
        self.assertEqual(sent[0].payload, pb.PrStateSet(set_para=0).SerializeToString())
        self.assertEqual(sent[0].payload, b"\x10\x00")

    async def test_set_ac_output(self):
        conn = make_conn()
        sent = capture(conn)
        await conn.setAcOutput(True)
        self.assertEqual((sent[0].cmdSet, sent[0].cmdId), (0x02, 0x48))
        self.assertEqual(sent[0].payload, pb.ACDsgSet(enable=1).SerializeToString())

    async def test_set_dc_output(self):
        conn = make_conn()
        sent = capture(conn)
        await conn.setDcOutput(False)
        self.assertEqual((sent[0].cmdSet, sent[0].cmdId), (0x02, 0x44))
        self.assertEqual(sent[0].payload, pb.DCSwitchSet(enable=0).SerializeToString())

    async def test_send_dpu_command_framing(self):
        conn = make_conn()
        sent = capture(conn)
        await conn.sendDpuCommand(0x02, 0x6A, pb.PrStateSet(set_self=1))
        p = sent[0]
        # framing constants from the HA integration
        self.assertEqual(p._version, 0x13)
        self.assertEqual((p._dsrc, p._ddst), (0x01, 0x01))


class StartupCommandDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_cmd = connect.CMD

    def tearDown(self):
        connect.CMD = self._orig_cmd

    def _stub(self, conn):
        calls = []

        async def pbox(set_self=None, set_para=None):
            calls.append(("pbox", set_self, set_para))

        async def ac(enable):
            calls.append(("ac", enable))

        async def dc(enable):
            calls.append(("dc", enable))

        conn.setParallelBox = pbox
        conn.setAcOutput = ac
        conn.setDcOutput = dc
        return calls

    async def test_pbox_self_off(self):
        conn = make_conn()
        calls = self._stub(conn)
        connect.CMD = "pbox_self_off"
        await conn.runStartupCommand()
        self.assertEqual(calls, [("pbox", 0, None)])

    async def test_ac_off(self):
        conn = make_conn()
        calls = self._stub(conn)
        connect.CMD = "ac_off"
        await conn.runStartupCommand()
        self.assertEqual(calls, [("ac", False)])

    async def test_fires_only_once(self):
        conn = make_conn()
        calls = self._stub(conn)
        connect.CMD = "dc_on"
        await conn.runStartupCommand()
        await conn.runStartupCommand()  # second call is a no-op
        self.assertEqual(calls, [("dc", True)])

    async def test_unknown_command_no_call(self):
        conn = make_conn()
        calls = self._stub(conn)
        connect.CMD = "bogus"
        await conn.runStartupCommand()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
