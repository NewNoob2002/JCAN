import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import jcan_mcp
from jcan import CanStreamParser, CONFIG_VALUE_OFFSET, HEADER, MODES, JCan, JCanError, LibUsb, crc8, packet, sdo_write


ROOT = Path(__file__).resolve().parent


def write_active_profile(path):
    path.write_text(
        """schema_version = 1
name = \"test-bus\"
enabled = true
approved = true
authorization = \"unit test\"
approved_at_utc = \"2026-09-02T00:00:00Z\"
serial = \"TEST\"
nominal_bitrate = 500000
data_bitrate = 2000000
fd_standard = 0
min_period_ms = 10
max_periodic_tasks = 4
cleanup_state = \"stopped\"
max_capture_ms = 100
max_capture_frames = 10

[expected_config]
can_speed = \"0c\"
can_customval = \"00 00 01 00 04 00 59 00 1e 00\"
fd_speed = \"06\"
fd_customval = \"00 00 03 00 04 00 1d 00 0a 00\"
standard = \"00\"
term_res = \"00\"
busoff_recovery = \"00\"
auto_retrans = \"00\"

[[frames]]
name = \"command\"
direction = \"tx\"
id = 0x123
extended = false
fd = false
brs = false
remote = false
periodic = true
dlc = [2]
data = [\"11 22\"]

[[frames]]
name = \"reply\"
direction = \"rx\"
id = 0x321
extended = false
fd = false
brs = false
remote = false
dlc = [2]
data = [\"33 44\"]

[[frames]]
name = \"sdo-upload\"
direction = \"tx\"
id = 0x601
extended = false
fd = false
brs = false
remote = false
dlc = [8]
data = [\"40 08 20 00 00 00 00 00\"]

[[frames]]
name = \"sdo-download-same\"
direction = \"tx\"
id = 0x601
extended = false
fd = false
brs = false
remote = false
dlc = [8]
data = [\"2B 08 20 00 F4 01 00 00\"]

[[frames]]
name = \"sdo-response\"
direction = \"rx\"
id = 0x581
extended = false
fd = false
brs = false
remote = false
dlc = [8]
""",
        encoding="utf-8",
    )


class FakePhysicalCan:
    def __init__(self, frames=(), send_delay=0):
        self.config = FakeUsb().original
        self.frames = list(frames)
        self.send_delay = send_delay
        self.started = []
        self.sent = []
        self.stopped = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def _config_get(self, name, _size):
        return self.config[name]

    def enable_receive(self):
        pass

    def start(self, mode):
        self.started.append(mode)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def send(self, can_id, data, **flags):
        if self.send_delay:
            time.sleep(self.send_delay)
        self.sent.append((can_id, data, flags))

    def receive(self, _timeout_ms):
        return self.frames.pop(0) if self.frames else b""


class SdoResponseCan:
    """HIL adapter that turns each scheduled SDO frame into a checked request/response transaction."""

    def __init__(self, serial):
        self.can = JCan(LibUsb(), serial)
        self.parser = CanStreamParser()
        self.responses = []

    def _config_get(self, name, size):
        return self.can._config_get(name, size)

    def start(self, mode):
        self.can.enable_receive()
        self.can.start(mode)

    def send(self, can_id, data, **flags):
        if can_id != 0x601 or data != bytes.fromhex("2B 08 20 00 C8 00 00 00") or any(flags.values()):
            raise JCanError("周期 SDO HIL 收到未授权帧")
        self.responses.append(sdo_write(self.can, self.parser, 1, 0x2008, 0, b"\xC8\x00"))

    def stop(self):
        self.can.stop()

    def close(self):
        self.can.close()


def raw_frame(can_id, data):
    raw = b"\xff\xaa" + bytes([len(data)]) + struct.pack("<IH", can_id, 1) + data
    return raw + bytes([crc8(raw)])


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

    async def run(self, _function, *_args, **_kwargs):
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
                            "jcan_bus_profile_status",
                            "jcan_capture",
                            "jcan_send_once",
                            "jcan_periodic_start",
                            "jcan_periodic_list",
                            "jcan_periodic_stop",
                            "jcan_sdo_read",
                            "jcan_sdo_u16_same_value_test",
                        },
                    )
                    self.assertEqual(schemas["jcan_get_config"]["required"], ["serial"])
                    self.assertEqual(
                        set(schemas["jcan_apply_config"]["properties"]),
                        {"serial", *jcan_mcp.CONFIG_ARGUMENTS},
                    )
                    self.assertEqual(schemas["jcan_apply_config"]["required"], ["serial"])
                    self.assertEqual(set(schemas["jcan_reboot"]["properties"]), {"serial", "timeout_s"})
                    self.assertEqual(
                        set(schemas["jcan_send_once"]["properties"]),
                        {"serial", "can_id", "data_hex", "fd", "remote", "brs", "extended"},
                    )
                    self.assertEqual(
                        set(schemas["jcan_capture"]["properties"]),
                        {"serial", "duration_ms", "max_frames"},
                    )
                    self.assertEqual(
                        set(schemas["jcan_periodic_start"]["properties"]),
                        {"serial", "can_id", "data_hex", "period_ms", "count", "fd", "remote", "brs", "extended"},
                    )
                    self.assertEqual(
                        schemas["jcan_periodic_start"]["required"],
                        ["serial", "can_id", "data_hex", "period_ms", "count"],
                    )
                    self.assertEqual(set(schemas["jcan_periodic_stop"]["properties"]), {"serial", "task_id"})
                    self.assertEqual(
                        set(schemas["jcan_sdo_read"]["properties"]),
                        {"serial", "node", "index", "subindex"},
                    )

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

                    result = await asyncio.wait_for(session.call_tool("jcan_bus_profile_status", {}), 5)
                    self.assertTrue(result.structuredContent["ok"])
                    self.assertTrue(result.structuredContent["data"]["enabled"])
                    self.assertTrue(result.structuredContent["data"]["approved"])
                    self.assertEqual(result.structuredContent["data"]["nominal_bitrate"], 500000)
                    self.assertEqual(result.structuredContent["data"]["frames"][0]["id"], 0x7FF)
                    self.assertFalse(result.structuredContent["data"]["frames"][0]["extended"])
                    self.assertFalse(result.structuredContent["data"]["frames"][0]["periodic"])
                    result = await asyncio.wait_for(
                        session.call_tool(
                            "jcan_send_once",
                            {
                                "serial": "207F346D5650", "can_id": 0x7FF,
                                "data_hex": "00 00 00 01", "extended": False,
                            },
                        ),
                        5,
                    )
                    self.assertFalse(result.structuredContent["ok"])


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
                request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
                    devices=FakeDevices(error), hardware_gate=asyncio.Lock(),
                    periodic=SimpleNamespace(is_active=lambda _serial: False),
                ))
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

    def test_physical_profile_send_and_capture(self):
        profile_path = Path(self.temp.name) / "profile.toml"
        write_active_profile(profile_path)
        profile = jcan_mcp.BusProfile.load(profile_path)
        self.assertTrue(profile.enabled and profile.approved)

        can = FakePhysicalCan()
        result = jcan_mcp._send_once(
            "TEST", 0x123, "11 22", fd=False, remote=False, brs=False, extended=False,
            profile_path=profile_path, can_factory=lambda _serial: can,
        )
        self.assertEqual(result["rule"], "command")
        self.assertEqual(can.started, [MODES["normal"]])
        self.assertTrue(can.stopped and len(can.sent) == 1)

        can = FakePhysicalCan()
        can.config = {**can.config, "term_res": b"\1"}
        with self.assertRaises(JCanError):
            jcan_mcp._send_once(
                "TEST", 0x123, "11 22", fd=False, remote=False, brs=False, extended=False,
                profile_path=profile_path, can_factory=lambda _serial: can,
            )
        self.assertFalse(can.started or can.sent)

        with self.assertRaises(JCanError):
            jcan_mcp._send_once(
                "TEST", 0x123, "11 23", fd=False, remote=False, brs=False, extended=False,
                profile_path=profile_path, can_factory=lambda _serial: self.fail("USB must not open"),
            )

        can = FakePhysicalCan([raw_frame(0x321, bytes.fromhex("33 44"))])
        result = jcan_mcp._capture(
            "TEST", 100, 1, profile_path=profile_path, can_factory=lambda _serial: can,
        )
        self.assertEqual((result["received_frames"], result["matched_frames"]), (1, 1))
        self.assertEqual(can.started, [MODES["silent"]])
        self.assertTrue(can.stopped)

    def test_profile_gate_rejects_before_usb(self):
        with self.assertRaises(JCanError):
            jcan_mcp._send_once(
                "207F346D5650", 0x7FF, "00 00 00 01", fd=False, remote=False, brs=False, extended=False,
                profile_path=ROOT / "jcan_bus_profile.toml", can_factory=lambda _serial: self.fail("USB must not open"),
            )

        profile_path = Path(self.temp.name) / "unapproved.toml"
        write_active_profile(profile_path)
        profile_path.write_text(profile_path.read_text().replace("approved = true", "approved = false"))
        with self.assertRaises(JCanError):
            jcan_mcp.BusProfile.load(profile_path)

    def test_sdo_read_and_same_value_write(self):
        profile_path = Path(self.temp.name) / "profile.toml"
        write_active_profile(profile_path)
        upload = bytes.fromhex("4B 08 20 00 F4 01 00 00")
        download = bytes.fromhex("60 08 20 00 00 00 00 00")

        can = FakePhysicalCan([raw_frame(0x581, upload)])
        result = jcan_mcp._sdo_read(
            "TEST", 1, 0x2008, 0, profile_path=profile_path, can_factory=lambda _serial: can,
        )
        self.assertEqual((result["value_unsigned"], result["value_hex"]), (500, "F4 01"))
        self.assertEqual(can.sent[0][0:2], (0x601, bytes.fromhex("40 08 20 00 00 00 00 00")))
        self.assertTrue(can.stopped)

        can = FakePhysicalCan([
            raw_frame(0x581, upload), raw_frame(0x581, download), raw_frame(0x581, upload),
        ])
        result = jcan_mcp._sdo_u16_same_value_test(
            "TEST", 1, 0x2008, 0, profile_path=profile_path, can_factory=lambda _serial: can,
        )
        self.assertEqual(result["value_unsigned"], 500)
        self.assertFalse(result["write_changed_value"] or result["eeprom_save"])
        self.assertEqual([item[1] for item in can.sent], [
            bytes.fromhex("40 08 20 00 00 00 00 00"),
            bytes.fromhex("2B 08 20 00 F4 01 00 00"),
            bytes.fromhex("40 08 20 00 00 00 00 00"),
        ])
        self.assertTrue(can.stopped)

    async def test_periodic_scheduler_completion_and_stop(self):
        profile_path = Path(self.temp.name) / "profile.toml"
        write_active_profile(profile_path)
        devices = jcan_mcp.DeviceManager()
        can = FakePhysicalCan(send_delay=0.025)
        manager = jcan_mcp.PeriodicManager(
            devices, (hardware_gate := asyncio.Lock()), profile_path=profile_path, can_factory=lambda _serial: can,
        )
        try:
            started = await manager.start(
                "TEST", 0x123, "11 22", 10, 3,
                fd=False, remote=False, brs=False, extended=False,
            )
            self.assertEqual(started["state"], "running")
            deadline = asyncio.get_running_loop().time() + 1
            while manager.runner is not None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            listing = await manager.list("TEST")
            completed = listing["recent"][-1]
            self.assertEqual((completed["state"], completed["sent_count"]), ("completed", 3))
            self.assertGreater(completed["missed_periods"], 0)
            self.assertEqual(can.started, [MODES["normal"]])
            self.assertTrue(can.stopped and can.closed)

            can = FakePhysicalCan()
            manager.can_factory = lambda _serial: can
            started = await manager.start(
                "TEST", 0x123, "11 22", 10, 100,
                fd=False, remote=False, brs=False, extended=False,
            )
            while len(can.sent) < 2:
                await asyncio.sleep(0.005)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
                devices=devices, hardware_gate=hardware_gate, periodic=manager,
            )))
            blocked = await jcan_mcp._hardware_call(ctx, "test", "TEST", self.fail, "must not run")
            self.assertFalse(blocked["ok"])
            stopped = await manager.stop("TEST", started["task_id"])
            sent = len(can.sent)
            await asyncio.sleep(0.03)
            self.assertEqual((stopped["state"], len(can.sent)), ("stopped", sent))
            self.assertTrue(can.stopped and can.closed)

            with self.assertRaises(JCanError):
                await manager.start(
                    "TEST", 0x123, "11 23", 10, 1,
                    fd=False, remote=False, brs=False, extended=False,
                )
        finally:
            await manager.close()
            devices.close()

    async def test_periodic_requires_explicit_profile_authorization(self):
        profile_path = Path(self.temp.name) / "profile.toml"
        write_active_profile(profile_path)
        profile_path.write_text(profile_path.read_text().replace("periodic = true\n", ""))
        devices = jcan_mcp.DeviceManager()
        manager = jcan_mcp.PeriodicManager(
            devices, asyncio.Lock(), profile_path=profile_path,
            can_factory=lambda _serial: self.fail("USB must not open"),
        )
        try:
            with self.assertRaises(JCanError):
                await manager.start(
                    "TEST", 0x123, "11 22", 10, 1,
                    fd=False, remote=False, brs=False, extended=False,
                )
        finally:
            await manager.close()
            devices.close()


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


@unittest.skipUnless(os.environ.get("JCAN_STAGE4_SEND_SERIAL"), "set JCAN_STAGE4_SEND_SERIAL after physical-bus safety preflight")
class McpStage4PhysicalHilTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_whitelisted_standard_frame(self):
        serial = os.environ["JCAN_STAGE4_SEND_SERIAL"]
        server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "jcan_mcp.py")], cwd=ROOT)
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                result = await asyncio.wait_for(
                    session.call_tool(
                        "jcan_send_once",
                        {
                            "serial": serial,
                            "can_id": 0x7FF,
                            "data_hex": "00 00 00 00",
                            "extended": False,
                        },
                    ),
                    10,
                )
                self.assertTrue(result.structuredContent["ok"])
                self.assertEqual(result.structuredContent["data"]["rule"], "tx-7ff-zero4")


@unittest.skipUnless(os.environ.get("JCAN_STAGE4_PERIODIC_SERIAL"), "set JCAN_STAGE4_PERIODIC_SERIAL after periodic-bus safety preflight")
class McpStage4PeriodicHilTest(unittest.IsolatedAsyncioTestCase):
    async def _run_case(self, serial, count, name):
        devices = jcan_mcp.DeviceManager()
        adapters = []

        def factory(selected):
            adapter = SdoResponseCan(selected)
            adapters.append(adapter)
            return adapter

        manager = jcan_mcp.PeriodicManager(devices, asyncio.Lock(), can_factory=factory)
        started_at = time.monotonic()
        try:
            before = await devices.run(jcan_mcp._sdo_read, serial, 1, 0x2008, 0)
            self.assertEqual(before["value_unsigned"], 200)
            started = await manager.start(
                serial, 0x601, "2B 08 20 00 C8 00 00 00", 50, count,
                fd=False, remote=False, brs=False, extended=False,
            )
            deadline = asyncio.get_running_loop().time() + count * 0.05 + 15
            while manager.runner is not None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
            self.assertIsNone(manager.runner, "周期任务未在截止时间内完成")
            listing = await manager.list(serial)
            result = next(item for item in listing["recent"] if item["task_id"] == started["task_id"])
            after = await devices.run(jcan_mcp._sdo_read, serial, 1, 0x2008, 0)
            elapsed_ms = (time.monotonic() - started_at) * 1000
            self.assertEqual((result["state"], result["sent_count"], result["missed_periods"]), ("completed", count, 0))
            self.assertEqual(len(adapters), 1)
            self.assertEqual(len(adapters[0].responses), count)
            self.assertTrue(all(response == bytes.fromhex("60 08 20 00 00 00 00 00") for response in adapters[0].responses))
            self.assertEqual(after["value_unsigned"], 200)
            self.assertLess(result["timing"]["jitter_ms"]["max"], 50)
            print(json.dumps({
                "case": name, "period_ms": 50, "requested_frames": count,
                "elapsed_ms": elapsed_ms, "sdo_responses": len(adapters[0].responses),
                "before": before["value_unsigned"], "after": after["value_unsigned"],
                "periodic": result,
            }, ensure_ascii=False))
        finally:
            await manager.close()
            devices.close()

    async def test_50ms_for_60s_and_1500_frames(self):
        serial = os.environ["JCAN_STAGE4_PERIODIC_SERIAL"]
        await self._run_case(serial, 1200, "50ms-for-60s")
        await self._run_case(serial, 1500, "50ms-for-1500-frames")


if __name__ == "__main__":
    unittest.main()
