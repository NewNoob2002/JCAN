import asyncio
import os
import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent


class McpStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_self_test(self):
        server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "jcan_mcp.py")], cwd=ROOT)
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                tools = await asyncio.wait_for(session.list_tools(), 5)
                self.assertEqual(
                    {tool.name for tool in tools.tools},
                    {
                        "jcan_scan",
                        "jcan_get_config",
                        "jcan_self_test",
                        "jcan_loopback_test",
                        "jcan_loopback_benchmark",
                    },
                )
                result = await asyncio.wait_for(session.call_tool("jcan_self_test", {}), 5)
                self.assertFalse(result.isError)
                self.assertTrue(result.structuredContent["ok"])
                self.assertTrue(result.structuredContent["data"]["passed"])

                result = await asyncio.wait_for(session.call_tool("jcan_get_config", {"serial": ""}), 5)
                self.assertFalse(result.isError)
                self.assertFalse(result.structuredContent["ok"])


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


if __name__ == "__main__":
    unittest.main()
