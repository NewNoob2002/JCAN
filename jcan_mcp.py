#!/usr/bin/env python3
"""Safety-bounded stdio MCP server for JooDevice JTool-CAN adapters."""

import asyncio
import json
import os
import struct
import sys
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from jcan import (
    CONFIG_FIELDS,
    JCan,
    JCanError,
    LibUsb,
    config_roundtrip_test,
    loopback_benchmark,
    run_loopback_test,
    self_test,
)


EVIDENCE_DIR = Path(os.environ.get("JCAN_EVIDENCE_DIR", Path(__file__).parent / "evidence" / "runtime"))
CONFIG_NAMES = {name for name, _ in CONFIG_FIELDS}
CONFIG_ARGUMENTS = {
    "nominal_speed",
    "data_speed",
    "nominal_prescaler",
    "nominal_sjw",
    "nominal_seg1",
    "nominal_seg2",
    "data_prescaler",
    "data_sjw",
    "data_seg1",
    "data_seg2",
    "fd_standard",
    "terminal_resistance",
    "busoff_recovery",
    "auto_retransmission",
    "hardware_version",
    "device_id",
}


class RecoverableOperationError(JCanError):
    def __init__(self, message: str, data: dict[str, Any]):
        super().__init__(message)
        self.data = data


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


def _read_config(can: JCan) -> dict[str, bytes]:
    return {name: can._config_get(name, size) for name, size in CONFIG_FIELDS}


def _hex_config(config: dict[str, bytes]) -> dict[str, str]:
    return {name: value.hex(" ") for name, value in config.items()}


def _get_config(serial: str) -> dict[str, str]:
    with JCan(LibUsb(), _require_serial(serial)) as can:
        return _hex_config(_read_config(can))


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


def _config_operations(changes: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    unknown = changes.keys() - CONFIG_ARGUMENTS
    if unknown:
        raise JCanError(f"不支持的配置字段: {', '.join(sorted(unknown))}")
    changes = {name: value for name, value in changes.items() if value is not None}
    if not changes:
        raise JCanError("至少指定一个配置字段")

    special = {name for name in ("hardware_version", "device_id") if name in changes}
    if special and len(changes) != 1:
        raise JCanError("hardware_version 和 device_id 必须分别单独配置")

    operations: list[tuple[str, tuple[Any, ...]]] = []
    for prefix, method in (("nominal", "set_nominal_custom_speed"), ("data", "set_data_custom_speed")):
        names = tuple(f"{prefix}_{part}" for part in ("prescaler", "sjw", "seg1", "seg2"))
        present = [name in changes for name in names]
        if any(present) and not all(present):
            raise JCanError(f"{prefix} 自定义时序必须同时提供 prescaler、sjw、seg1、seg2")
        if all(present):
            if f"{prefix}_speed" in changes:
                raise JCanError(f"{prefix}_speed 不能与 {prefix} 自定义时序同时设置")
            args = tuple(changes[name] for name in names)
            if any(not isinstance(value, int) or isinstance(value, bool) for value in args):
                raise JCanError(f"{prefix} 自定义时序参数必须是整数")
            if prefix == "nominal" and not (1 <= args[0] <= 512 and 1 <= args[1] <= 128 and 2 <= args[2] <= 256 and 2 <= args[3] <= 128 and args[1] <= args[3]):
                raise JCanError("经典 CAN 自定义时序范围: prescaler 1..512, sjw 1..128, seg1 2..256, seg2 2..128, sjw <= seg2")
            if prefix == "data" and not (1 <= args[0] <= 32 and 1 <= args[1] <= 16 and 1 <= args[2] <= 32 and 1 <= args[3] <= 16 and args[1] <= args[3]):
                raise JCanError("CAN FD 数据相位时序范围: prescaler 1..32, sjw 1..16, seg1 1..32, seg2 1..16, sjw <= seg2")
            operations.append((method, args))

    for field, method, maximum in (
        ("nominal_speed", "set_nominal_speed", 0xFF),
        ("data_speed", "set_data_speed", 0xFF),
        ("fd_standard", "set_fd_standard", 1),
        ("terminal_resistance", "set_terminal_resistance", 1),
        ("busoff_recovery", "set_busoff_auto_recovery", 1),
        ("auto_retransmission", "set_auto_retransmission", 1),
        ("device_id", "set_device_id", 0xFFFF),
    ):
        if field in changes:
            value = changes[field]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
                raise JCanError(f"{field} 范围必须是 0..{maximum}")
            operations.append((method, (value,)))

    if "hardware_version" in changes:
        version = changes["hardware_version"]
        if not isinstance(version, str):
            raise JCanError("hardware_version 必须是字符串")
        try:
            encoded = version.encode("ascii")
        except UnicodeEncodeError as exc:
            raise JCanError("硬件版本必须是 ASCII 字符") from exc
        if len(encoded) > 9:
            raise JCanError("硬件版本最多 9 个 ASCII 字符")
        operations.append(("set_hardware_version", (version,)))
    return operations


def _expected_config(baseline: dict[str, bytes], operations: list[tuple[str, tuple[Any, ...]]]) -> dict[str, bytes]:
    expected = dict(baseline)
    for method, args in operations:
        value = args[0]
        if method == "set_nominal_speed":
            expected["can_speed"] = bytes([value])
            expected["can_customval"] = b"\0\0" + expected["can_customval"][2:]
        elif method == "set_data_speed":
            expected["fd_speed"] = bytes([value])
            expected["fd_customval"] = b"\0\0" + expected["fd_customval"][2:]
        elif method == "set_nominal_custom_speed":
            expected["can_customval"] = struct.pack("<5H", 1, *args)
        elif method == "set_data_custom_speed":
            expected["fd_customval"] = struct.pack("<5H", 1, *args)
        elif method == "set_fd_standard":
            expected["standard"] = bytes([value])
        elif method == "set_terminal_resistance":
            expected["term_res"] = bytes([value])
        elif method == "set_busoff_auto_recovery":
            expected["busoff_recovery"] = bytes([value])
        elif method == "set_auto_retransmission":
            expected["auto_retrans"] = bytes([value])
        elif method == "set_hardware_version":
            expected["hardware_version"] = value.encode("ascii").ljust(10, b"\0")
        elif method == "set_device_id":
            expected["id"] = struct.pack("<H", value)
    return expected


def _restore_config(can: JCan, baseline: dict[str, bytes]) -> dict[str, Any]:
    errors = []
    for name, _ in CONFIG_FIELDS:
        try:
            can._config_set(name, baseline[name])
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    try:
        current = _read_config(can)
        errors.extend(f"{name}: 回读不匹配" for name in CONFIG_NAMES if current[name] != baseline[name])
    except Exception as exc:
        errors.append(f"恢复后回读失败: {exc}")
    return {"ok": not errors, "errors": errors}


def _apply_config(serial: str, changes: dict[str, Any], usb_factory: Callable[[], LibUsb] = LibUsb) -> dict[str, Any]:
    operations = _config_operations(changes)
    with JCan(usb_factory(), _require_serial(serial)) as can:
        baseline = _read_config(can)
        expected = _expected_config(baseline, operations)
        try:
            for method, args in operations:
                getattr(can, method)(*args)
            actual = _read_config(can)
            mismatches = [name for name in CONFIG_NAMES if actual[name] != expected[name]]
            if mismatches:
                raise JCanError(f"配置回读不匹配: {', '.join(sorted(mismatches))}")
        except Exception as exc:
            recovery = _restore_config(can, baseline)
            raise RecoverableOperationError(
                f"配置失败，基线恢复{'成功' if recovery['ok'] else '失败'}: {exc}",
                {"restored": recovery["ok"], "recovery_errors": recovery["errors"]},
            ) from exc
        changed = [name for name in CONFIG_NAMES if actual[name] != baseline[name]]
        return {
            "changed_fields": sorted(changed),
            "before": _hex_config({name: baseline[name] for name in changed}),
            "after": _hex_config({name: actual[name] for name in changed}),
            "restored": None,
        }


def _config_roundtrip(serial: str, usb_factory: Callable[[], LibUsb] = LibUsb) -> dict[str, Any]:
    with JCan(usb_factory(), _require_serial(serial)) as can:
        baseline = _read_config(can)
        try:
            config_roundtrip_test(can, verbose=False)
            actual = _read_config(can)
            if actual != baseline:
                raise JCanError("配置往返后未恢复完整基线")
        except Exception as exc:
            recovery = _restore_config(can, baseline)
            raise RecoverableOperationError(
                f"配置往返失败，基线恢复{'成功' if recovery['ok'] else '失败'}: {exc}",
                {"restored": recovery["ok"], "recovery_errors": recovery["errors"]},
            ) from exc
        return {"restored": True, "config": _hex_config(actual)}


def _reboot(serial: str, timeout_s: float, usb_factory: Callable[[], LibUsb] = LibUsb, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    serial = _require_serial(serial)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 1 <= timeout_s <= 30:
        raise JCanError("重启等待时间范围必须是 1..30 秒")
    usb = usb_factory()
    with JCan(usb, serial) as can:
        can.reboot()
    deadline = time.monotonic() + timeout_s
    seen_offline = False
    while time.monotonic() < deadline:
        match = next(
            ((bus, address) for dev, desc, bus, address in usb.devices() if usb.serial(dev, desc) == serial),
            None,
        )
        if match is None:
            seen_offline = True
        elif seen_offline:
            return {"reenumerated": True, "bus": match[0], "address": match[1]}
        sleep(0.1)
    raise JCanError(f"重启后 {timeout_s:g} 秒内未重新枚举设备 {serial}")


def _record_evidence(result: dict[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = EVIDENCE_DIR / f"{timestamp}-{time.time_ns()}-{result['operation']}.json"
    result["evidence_path"] = str(path.resolve())
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _result(operation: str, serial: str | None, *, data: Any = None, error: Exception | None = None) -> dict[str, Any]:
    result = {
        "ok": error is None,
        "serial": serial,
        "operation": operation,
        "data": data,
        "warnings": [] if error is None else [str(error)],
        "evidence_path": None,
    }
    try:
        _record_evidence(result)
    except OSError as exc:
        result["warnings"].append(f"证据写入失败: {exc}")
        result["evidence_path"] = None
    return result


async def _hardware_call(ctx: Context, operation: str, serial: str | None, function: Callable[..., Any], *args: Any) -> dict[str, Any]:
    try:
        data = await ctx.request_context.lifespan_context.devices.run(function, *args)
        return _result(operation, serial, data=data)
    except (JCanError, OSError, ValueError) as exc:
        return _result(operation, serial, data=getattr(exc, "data", None), error=exc)
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


@mcp.tool()
async def jcan_apply_config(
    serial: str,
    ctx: Context,
    nominal_speed: int | None = None,
    data_speed: int | None = None,
    nominal_prescaler: int | None = None,
    nominal_sjw: int | None = None,
    nominal_seg1: int | None = None,
    nominal_seg2: int | None = None,
    data_prescaler: int | None = None,
    data_sjw: int | None = None,
    data_seg1: int | None = None,
    data_seg2: int | None = None,
    fd_standard: int | None = None,
    terminal_resistance: int | None = None,
    busoff_recovery: int | None = None,
    auto_retransmission: int | None = None,
    hardware_version: str | None = None,
    device_id: int | None = None,
) -> dict[str, Any]:
    """Apply explicit JCAN configuration fields with readback and rollback on failure."""
    arguments = locals()
    changes = {name: arguments[name] for name in CONFIG_ARGUMENTS}
    return await _hardware_call(ctx, "apply_config", serial, _apply_config, serial, changes)


@mcp.tool()
async def jcan_config_roundtrip_test(serial: str, ctx: Context) -> dict[str, Any]:
    """Write, verify, and restore every reversible JCAN configuration field."""
    return await _hardware_call(ctx, "config_roundtrip_test", serial, _config_roundtrip, serial)


@mcp.tool()
async def jcan_reboot(serial: str, ctx: Context, timeout_s: float = 10.0) -> dict[str, Any]:
    """Reboot one explicit adapter and verify re-enumeration by serial number."""
    return await _hardware_call(ctx, "reboot", serial, _reboot, serial, timeout_s)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
