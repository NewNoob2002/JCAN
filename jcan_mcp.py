#!/usr/bin/env python3
"""Read-only stdio MCP server for JooDevice JTool-CAN adapters."""

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from jcan import (
    CONFIG_FIELDS,
    JCan,
    JCanError,
    LibUsb,
    loopback_benchmark,
    run_loopback_test,
    self_test,
)


class DeviceManager:
    """Serialize all libusb work onto one thread."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jcan-usb")

    async def run(self, function: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, lambda: function(*args))

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


@dataclass
class AppContext:
    devices: DeviceManager


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    devices = DeviceManager()
    try:
        yield AppContext(devices)
    finally:
        devices.close()


mcp = FastMCP("JCAN", lifespan=lifespan)


def _require_serial(serial: str) -> str:
    serial = serial.strip()
    if not serial:
        raise JCanError("必须明确指定 JCAN USB 序列号")
    return serial


def _scan_devices() -> list[dict[str, Any]]:
    usb = LibUsb()
    return [
        {
            "serial": usb.serial(dev, desc),
            "bus": bus,
            "address": address,
            "vid": f"{desc.idVendor:04x}",
            "pid": f"{desc.idProduct:04x}",
        }
        for dev, desc, bus, address in usb.devices()
    ]


def _get_config(serial: str) -> dict[str, str]:
    with JCan(LibUsb(), _require_serial(serial)) as can:
        return {name: can._config_get(name, size).hex(" ") for name, size in CONFIG_FIELDS}


def _loopback(serial: str) -> list[dict[str, Any]]:
    with JCan(LibUsb(), _require_serial(serial)) as can:
        return [
            {
                "id": frame["id"],
                "data": frame["data"].hex(" ").upper(),
                "fd": frame["fd"],
                "remote": frame["remote"],
                "brs": frame["brs"],
                "extended": frame["extended"],
                "timestamp": frame["timestamp"],
            }
            for frame in run_loopback_test(can)
        ]


def _benchmark(serial: str, count: int) -> dict[str, Any]:
    if not 1 <= count <= 10000:
        raise JCanError("帧数范围必须是 1..10000")
    with JCan(LibUsb(), _require_serial(serial)) as can:
        return loopback_benchmark(can, count)


def _result(operation: str, serial: str | None, *, data: Any = None, error: Exception | None = None) -> dict[str, Any]:
    return {
        "ok": error is None,
        "serial": serial,
        "operation": operation,
        "data": data,
        "warnings": [] if error is None else [str(error)],
        "evidence_path": None,
    }


async def _hardware_call(ctx: Context, operation: str, serial: str | None, function: Callable[..., Any], *args: Any) -> dict[str, Any]:
    try:
        data = await ctx.request_context.lifespan_context.devices.run(function, *args)
        return _result(operation, serial, data=data)
    except (JCanError, OSError, ValueError) as exc:
        return _result(operation, serial, error=exc)
    except Exception as exc:  # keep the stdio server alive on unexpected adapter failures
        print(f"jcan-mcp: {operation} failed: {exc}", file=sys.stderr)
        return _result(operation, serial, error=RuntimeError("unexpected JCAN server error"))


@mcp.tool()
async def jcan_scan(ctx: Context) -> dict[str, Any]:
    """Enumerate connected JTool-CAN adapters without changing device state."""
    return await _hardware_call(ctx, "scan", None, _scan_devices)


@mcp.tool()
async def jcan_get_config(serial: str, ctx: Context) -> dict[str, Any]:
    """Read the raw JCAN configuration for one explicit USB serial number."""
    return await _hardware_call(ctx, "get_config", serial, _get_config, serial)


@mcp.tool()
async def jcan_self_test() -> dict[str, Any]:
    """Run protocol and serialization checks without accessing hardware."""
    try:
        self_test(verbose=False)
        return _result("self_test", None, data={"passed": True})
    except Exception as exc:
        return _result("self_test", None, error=exc)


@mcp.tool()
async def jcan_loopback_test(serial: str, ctx: Context) -> dict[str, Any]:
    """Run the bounded three-frame internal silent-loopback test on one adapter."""
    return await _hardware_call(ctx, "loopback_test", serial, _loopback, serial)


@mcp.tool()
async def jcan_loopback_benchmark(serial: str, ctx: Context, count: int = 100) -> dict[str, Any]:
    """Benchmark bounded internal silent-loopback round trips; count must be 1..10000."""
    return await _hardware_call(ctx, "loopback_benchmark", serial, _benchmark, serial, count)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
