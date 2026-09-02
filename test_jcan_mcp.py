import asyncio
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import jcan_mcp
from jcan import CONFIG_VALUE_OFFSET, HEADER, JCanError, packet


ROOT = Path(__file__).resolve().parent


class FakeUsb:
    def __init__(self, failure=None, *, corrupt_verify=False, fail_restore=False):
        self.original = {
            "can_speed": b"\x0c",
            "can_customval": struct.pack("<5H", 0, 1, 4, 89, 30),
            "fd_speed": b"\x06",
            "fd_customval": struct.pack("<5H", 0, 3, 4, 29, 10),
            "standard": b"\0",
            "term_res": b"\0",
            "busoff_recovery": b"\0",
            "auto_retrans": b"\0",
            "hardware_version": b"\0" * 10,
            "id": b"\0\0",
        }
        self.state = dict(self.original)
        self.failure = failure
        self.corrupt_verify = corrupt_verify
        self.fail_restore = fail_restore
        self.failure_used = False
        self.corrupt_used = False
        self.rebooted = False
        self.enumeration_checks = 0
        self.reply = b""

    def open(self, serial):
        if not serial:
            raise JCanError("missing serial")
        return object(), serial, 1, 2

    def close(self, _handle):
        pass

    def devices(self):
        if not self.rebooted:
            return []
        self.enumeration_checks += 1
        if self.enumeration_checks == 1:
            return []
        return [(object(), SimpleNamespace(idVendor=0xFFFF, idProduct=4), 1, 3)]

    def serial(self, _dev, _desc):
        return "TEST"

    def transfer_out(self, _handle, _endpoint, data, timeout_ms=4500):
        _, _, _, command, length = HEADER.unpack_from(data)
        payload = data[HEADER.size:HEADER.size + length]
        if command == 1:
            name = payload.rstrip(b"\0").decode()
            value = self.state[name]
            if name == "term_res" and value == b"\1" and self.corrupt_verify and not self.corrupt_used:
                value = b"\0"
                self.corrupt_used = True
            body = bytearray(CONFIG_VALUE_OFFSET + len(value))
            body[0] = 1
            struct.pack_into("<H", body, 5, len(value))
            body[CONFIG_VALUE_OFFSET:] = value
            self.reply = packet(0, 1, body, response=1)
        elif command == 2:
            key, value = payload.split(b"\0", 1)
            name = key.decode()
            restore_failure = self.fail_restore and name == "term_res" and value == self.original[name] and self.state[name] != value
            if restore_failure:
                self.reply = packet(0, 2, b"\0", response=1)
            elif self.failure and not self.failure_used:
                self.failure_used = True
                if self.failure == "timeout":
                    self.reply = OSError(110, "timeout")
                elif self.failure == "short":
                    self.reply = b"J"
                else:
                    self.reply = packet(0, 2, b"\0", response=1)
            else:
                self.state[name] = value
                self.reply = packet(0, 2, b"\1", response=1)
        elif command == 3:
            self.rebooted = True
            self.reply = packet(0, 3, b"\1", response=1)

    def transfer_in(self, _handle, _endpoint, size=4096, timeout_ms=4500, allow_timeout=False):
        if isinstance(self.reply, Exception):
            error, self.reply = self.reply, b""
            raise error
        return self.reply


class FakeDevices:
    def __init__(self, error):
        self.error = error

    async def run(self, _function, *_args):
        raise self.error


class McpStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_self_test(self):
        with tempfile.TemporaryDirectory() as evidence_dir:
            server = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "jcan_mcp.py")],
                cwd=ROOT,
                env={**os.environ, "JCAN_EVIDENCE_DIR": evidence_dir},
            )
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), 5)
                    tools = await asyncio.wait_for(session.list_tools(), 5)
                    schemas = {tool.name: tool.inputSchema for tool in tools.tools}
                    self.assertEqual(
                        set(schemas),
                        {
                            "jcan_scan",
                            "jcan_get_config",
                            "jcan_self_test",
                            "jcan_loopback_test",
                            "jcan_loopback_benchmark",
                            "jcan_apply_config",
                            "jcan_config_roundtrip_test",
                            "jcan_reboot",
                        },
                    )
                    self.assertEqual(schemas["jcan_get_config"]["required"], ["serial"])
                    self.assertEqual(
                        set(schemas["jcan_apply_config"]["properties"]),
                        {"serial", *jcan_mcp.CONFIG_ARGUMENTS},
                    )
                    self.assertEqual(schemas["jcan_apply_config"]["required"], ["serial"])
                    self.assertEqual(set(schemas["jcan_reboot"]["properties"]), {"serial", "timeout_s"})

                    result = await asyncio.wait_for(session.call_tool("jcan_self_test", {}), 5)
                    self.assertFalse(result.isError)
                    self.assertTrue(result.structuredContent["ok"])
                    self.assertTrue(result.structuredContent["data"]["passed"])
                    self.assertTrue(Path(result.structuredContent["evidence_path"]).is_file())

                    result = await asyncio.wait_for(session.call_tool("jcan_get_config", {"serial": ""}), 5)
                    self.assertFalse(result.isError)
                    self.assertFalse(result.structuredContent["ok"])

                    result = await asyncio.wait_for(session.call_tool("jcan_apply_config", {"serial": "TEST"}), 5)
                    self.assertFalse(result.isError)
                    self.assertFalse(result.structuredContent["ok"])
                    result = await asyncio.wait_for(session.call_tool("jcan_self_test", {}), 5)
                    self.assertTrue(result.structuredContent["ok"])


class McpHostTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.evidence_patch = patch.object(jcan_mcp, "EVIDENCE_DIR", Path(self.temp.name))
        self.evidence_patch.start()

    def tearDown(self):
        self.evidence_patch.stop()
        self.temp.cleanup()

    async def test_structured_adapter_errors(self):
        for error in (JCanError("未找到设备"), OSError(13, "权限不足"), JCanError("LIBUSB_ERROR_NO_DEVICE")):
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(lifespan_context=SimpleNamespace(devices=FakeDevices(error)))
            )
            result = await jcan_mcp._hardware_call(ctx, "test", "TEST", lambda: None)
            self.assertFalse(result["ok"])
            self.assertTrue(result["warnings"])
            self.assertTrue(Path(result["evidence_path"]).is_file())

    def test_apply_config_success_and_failures(self):
        usb = FakeUsb()
        result = jcan_mcp._apply_config(
            "TEST",
            {
                "nominal_speed": 13,
                "data_prescaler": 3,
                "data_sjw": 4,
                "data_seg1": 29,
                "data_seg2": 10,
                "fd_standard": 1,
                "terminal_resistance": 1,
            },
            lambda: usb,
        )
        self.assertEqual(result["changed_fields"], ["can_speed", "fd_customval", "standard", "term_res"])
        self.assertIsNone(result["restored"])

        for failure in ("refusal", "timeout", "short"):
            usb = FakeUsb(failure)
            with self.assertRaises(jcan_mcp.RecoverableOperationError) as caught:
                jcan_mcp._apply_config("TEST", {"terminal_resistance": 1}, lambda: usb)
            self.assertTrue(caught.exception.data["restored"])
            self.assertEqual(usb.state, usb.original)

        usb = FakeUsb(corrupt_verify=True, fail_restore=True)
        with self.assertRaises(jcan_mcp.RecoverableOperationError) as caught:
            jcan_mcp._apply_config("TEST", {"terminal_resistance": 1}, lambda: usb)
        self.assertFalse(caught.exception.data["restored"])
        self.assertTrue(caught.exception.data["recovery_errors"])

    def test_roundtrip_and_reboot(self):
        usb = FakeUsb()
        result = jcan_mcp._config_roundtrip("TEST", lambda: usb)
        self.assertTrue(result["restored"])
        self.assertEqual(usb.state, usb.original)

        usb = FakeUsb()
        result = jcan_mcp._reboot("TEST", 1, lambda: usb, lambda _seconds: None)
        self.assertTrue(result["reenumerated"])
        self.assertTrue(usb.rebooted)


@unittest.skipUnless(os.environ.get("JCAN_TEST_SERIAL"), "set JCAN_TEST_SERIAL for HIL")
class McpHilTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_and_internal_loopback(self):
        serial = os.environ["JCAN_TEST_SERIAL"]
        server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "jcan_mcp.py")], cwd=ROOT)
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)

                result = await asyncio.wait_for(session.call_tool("jcan_scan", {}), 5)
                self.assertTrue(result.structuredContent["ok"])
                self.assertIn(serial, {device["serial"] for device in result.structuredContent["data"]})

                result = await asyncio.wait_for(session.call_tool("jcan_get_config", {"serial": serial}), 5)
                self.assertTrue(result.structuredContent["ok"])
                self.assertEqual(len(result.structuredContent["data"]), 10)

                result = await asyncio.wait_for(session.call_tool("jcan_loopback_test", {"serial": serial}), 10)
                self.assertTrue(result.structuredContent["ok"])
                self.assertEqual(len(result.structuredContent["data"]), 3)

                result = await asyncio.wait_for(
                    session.call_tool("jcan_loopback_benchmark", {"serial": serial, "count": 10}), 10
                )
                self.assertTrue(result.structuredContent["ok"])
                self.assertEqual(result.structuredContent["data"]["count"], 10)


@unittest.skipUnless(os.environ.get("JCAN_STAGE3_TEST_SERIAL"), "set JCAN_STAGE3_TEST_SERIAL after safety preflight")
class McpStage3HilTest(unittest.IsolatedAsyncioTestCase):
    async def test_reversible_config_and_reboot(self):
        serial = os.environ["JCAN_STAGE3_TEST_SERIAL"]
        server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "jcan_mcp.py")], cwd=ROOT)
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                config = (await asyncio.wait_for(
                    session.call_tool("jcan_get_config", {"serial": serial}), 10
                )).structuredContent["data"]
                result = await asyncio.wait_for(
                    session.call_tool(
                        "jcan_apply_config",
                        {
                            "serial": serial,
                            "fd_standard": int(config["standard"], 16),
                            "terminal_resistance": int(config["term_res"], 16),
                            "busoff_recovery": int(config["busoff_recovery"], 16),
                            "auto_retransmission": int(config["auto_retrans"], 16),
                        },
                    ),
                    20,
                )
                self.assertTrue(result.structuredContent["ok"])

                result = await asyncio.wait_for(
                    session.call_tool("jcan_config_roundtrip_test", {"serial": serial}), 30
                )
                self.assertTrue(result.structuredContent["ok"])
                self.assertTrue(result.structuredContent["data"]["restored"])

                result = await asyncio.wait_for(
                    session.call_tool("jcan_reboot", {"serial": serial, "timeout_s": 15}), 20
                )
                self.assertTrue(result.structuredContent["ok"])
                self.assertTrue(result.structuredContent["data"]["reenumerated"])


if __name__ == "__main__":
    unittest.main()
