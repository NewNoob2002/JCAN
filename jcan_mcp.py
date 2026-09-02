#!/usr/bin/env python3
"""Safety-bounded stdio MCP server for JooDevice JTool-CAN adapters."""

import asyncio
import json
import os
import struct
import sys
import time
import tomllib
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from jcan import (
    DLC_LENGTHS,
    MODES,
    CanStreamParser,
    CONFIG_FIELDS,
    JCan,
    JCanError,
    LibUsb,
    config_roundtrip_test,
    loopback_benchmark,
    sdo_read,
    sdo_write,
    run_loopback_test,
    self_test,
)


EVIDENCE_DIR = Path(os.environ.get("JCAN_EVIDENCE_DIR", Path(__file__).parent / "evidence" / "runtime"))
PROFILE_PATH = Path(os.environ.get("JCAN_BUS_PROFILE", Path(__file__).parent / "jcan_bus_profile.toml"))
CONFIG_NAMES = {name for name, _ in CONFIG_FIELDS}
PROFILE_CONFIG_FIELDS = {
    name: size for name, size in CONFIG_FIELDS
    if name in {
        "can_speed", "can_customval", "fd_speed", "fd_customval", "standard",
        "term_res", "busoff_recovery", "auto_retrans",
    }
}
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


@dataclass(frozen=True)
class FrameRule:
    name: str
    direction: str
    can_id: int
    extended: bool
    fd: bool
    brs: bool
    remote: bool
    periodic: bool
    dlc: frozenset[int]
    payloads: frozenset[bytes]

    def matches(self, can_id: int, data: bytes, *, extended: bool, fd: bool, brs: bool, remote: bool) -> bool:
        return (
            self.can_id == can_id
            and self.extended == extended
            and self.fd == fd
            and self.brs == brs
            and self.remote == remote
            and len(data) in self.dlc
            and (not self.payloads or data in self.payloads)
        )


@dataclass(frozen=True)
class BusProfile:
    path: Path
    name: str
    enabled: bool
    approved: bool
    authorization: str
    approved_at_utc: str
    serial: str
    nominal_bitrate: int
    data_bitrate: int
    fd_standard: int
    min_period_ms: int
    max_periodic_tasks: int
    cleanup_state: str
    max_capture_ms: int
    max_capture_frames: int
    expected_config: dict[str, bytes]
    frames: tuple[FrameRule, ...]

    @classmethod
    def load(cls, path: Path = PROFILE_PATH) -> "BusProfile":
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise JCanError(f"物理总线 profile 不存在: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise JCanError(f"物理总线 profile TOML 无效: {exc}") from exc

        allowed = {
            "schema_version", "name", "enabled", "approved", "authorization", "approved_at_utc",
            "serial", "nominal_bitrate",
            "data_bitrate", "fd_standard", "min_period_ms", "max_periodic_tasks",
            "cleanup_state", "max_capture_ms", "max_capture_frames", "expected_config", "frames",
        }
        unknown = raw.keys() - allowed
        if unknown:
            raise JCanError(f"profile 包含未知字段: {', '.join(sorted(unknown))}")
        if raw.get("schema_version") != 1:
            raise JCanError("profile schema_version 必须是 1")

        def integer(name: str, minimum: int = 0, maximum: int | None = None) -> int:
            value = raw.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
                ceiling = f"..{maximum}" if maximum is not None else " 或更大"
                raise JCanError(f"profile {name} 必须是 {minimum}{ceiling} 的整数")
            return value

        enabled = raw.get("enabled")
        approved = raw.get("approved")
        if not isinstance(enabled, bool) or not isinstance(approved, bool):
            raise JCanError("profile enabled/approved 必须是布尔值")
        name = raw.get("name")
        authorization = raw.get("authorization", "")
        approved_at_utc = raw.get("approved_at_utc", "")
        serial = raw.get("serial")
        cleanup_state = raw.get("cleanup_state")
        if not all(isinstance(value, str) for value in (name, authorization, approved_at_utc, serial)) or not name.strip():
            raise JCanError("profile name 必须非空，授权信息和 serial 必须是字符串")
        if cleanup_state != "stopped":
            raise JCanError("profile cleanup_state 仅允许 stopped")

        expected_raw = raw.get("expected_config", {})
        if not isinstance(expected_raw, dict) or expected_raw.keys() - PROFILE_CONFIG_FIELDS.keys():
            raise JCanError("profile expected_config 包含未知字段")
        expected_config = {}
        for field, value in expected_raw.items():
            if not isinstance(value, str):
                raise JCanError(f"profile expected_config.{field} 必须是十六进制字符串")
            try:
                parsed = bytes.fromhex(value)
            except ValueError as exc:
                raise JCanError(f"profile expected_config.{field} 不是有效十六进制") from exc
            if len(parsed) != PROFILE_CONFIG_FIELDS[field]:
                raise JCanError(f"profile expected_config.{field} 长度必须是 {PROFILE_CONFIG_FIELDS[field]} 字节")
            expected_config[field] = parsed

        rules = []
        for index, item in enumerate(raw.get("frames", [])):
            if not isinstance(item, dict):
                raise JCanError(f"profile frames[{index}] 必须是表")
            frame_allowed = {"name", "direction", "id", "extended", "fd", "brs", "remote", "periodic", "dlc", "data"}
            extra = item.keys() - frame_allowed
            if extra:
                raise JCanError(f"profile frames[{index}] 包含未知字段: {', '.join(sorted(extra))}")
            frame_name = item.get("name")
            direction = item.get("direction")
            can_id = item.get("id")
            flags = [item.get(flag, False) for flag in ("extended", "fd", "brs", "remote")]
            periodic = item.get("periodic", False)
            dlc = item.get("dlc")
            if not isinstance(frame_name, str) or not frame_name or direction not in {"tx", "rx"}:
                raise JCanError(f"profile frames[{index}] 需要非空 name 和 tx/rx direction")
            if not isinstance(can_id, int) or isinstance(can_id, bool) or not 0 <= can_id <= (0x1FFFFFFF if flags[0] else 0x7FF):
                raise JCanError(f"profile frames[{index}] CAN ID 超出范围")
            if any(not isinstance(flag, bool) for flag in flags) or not isinstance(periodic, bool):
                raise JCanError(f"profile frames[{index}] 帧标志必须是布尔值")
            extended, fd, brs, remote = flags
            if brs and not fd or remote and fd:
                raise JCanError(f"profile frames[{index}] BRS/remote 标志组合无效")
            if not isinstance(dlc, list) or not dlc or any(not isinstance(value, int) or isinstance(value, bool) for value in dlc):
                raise JCanError(f"profile frames[{index}] dlc 必须是非空整数数组")
            valid_lengths = set(DLC_LENGTHS if fd else range(9))
            if not set(dlc) <= valid_lengths or remote and set(dlc) != {0}:
                raise JCanError(f"profile frames[{index}] DLC 与帧类型不匹配")
            payloads = set()
            for value in item.get("data", []):
                if not isinstance(value, str):
                    raise JCanError(f"profile frames[{index}] data 必须是十六进制字符串数组")
                try:
                    payload = bytes.fromhex(value)
                except ValueError as exc:
                    raise JCanError(f"profile frames[{index}] data 不是有效十六进制") from exc
                if len(payload) not in dlc:
                    raise JCanError(f"profile frames[{index}] payload 长度未列入 DLC")
                payloads.add(payload)
            if direction == "tx" and not remote and not payloads:
                raise JCanError(f"profile frames[{index}] TX 必须明确允许的 data")
            if remote and payloads:
                raise JCanError(f"profile frames[{index}] remote 帧不能包含 data")
            rules.append(FrameRule(frame_name, direction, can_id, extended, fd, brs, remote, periodic, frozenset(dlc), frozenset(payloads)))

        profile = cls(
            path, name.strip(), enabled, approved, authorization.strip(), approved_at_utc.strip(), serial.strip(),
            integer("nominal_bitrate"),
            integer("data_bitrate"), integer("fd_standard", 0, 1), integer("min_period_ms", 1),
            integer("max_periodic_tasks", 1), cleanup_state, integer("max_capture_ms", 1),
            integer("max_capture_frames", 1), expected_config, tuple(rules),
        )
        if profile.enabled:
            if not profile.approved or not profile.authorization or not profile.approved_at_utc or not profile.serial or profile.nominal_bitrate == 0:
                raise JCanError("启用 profile 需要批准、授权来源/时间、明确 serial 和 nominal_bitrate")
            if set(profile.expected_config) != set(PROFILE_CONFIG_FIELDS):
                raise JCanError("启用 profile 必须提供完整 expected_config")
            if profile.expected_config["standard"] != bytes([profile.fd_standard]):
                raise JCanError("profile fd_standard 与 expected_config.standard 不一致")
            if any(rule.fd for rule in profile.frames) and profile.data_bitrate == 0:
                raise JCanError("包含 CAN FD 帧的 profile 必须明确 data_bitrate")
            if not profile.frames:
                raise JCanError("启用 profile 必须至少定义一条 frame 规则")
        return profile

    def require_enabled(self, serial: str) -> None:
        if not self.enabled or not self.approved:
            raise JCanError(f"物理总线 profile {self.name} 未启用并批准")
        if _require_serial(serial) != self.serial:
            raise JCanError(f"序列号 {serial} 不匹配 profile {self.serial}")

    def verify_config(self, can: JCan) -> None:
        actual = _read_config(can)
        mismatches = [name for name, expected in self.expected_config.items() if actual[name] != expected]
        if mismatches:
            raise JCanError(f"设备配置不匹配 profile: {', '.join(sorted(mismatches))}")

    def tx_rule(self, can_id: int, data: bytes, *, extended: bool, fd: bool, brs: bool, remote: bool) -> FrameRule:
        for rule in self.frames:
            if rule.direction == "tx" and rule.matches(can_id, data, extended=extended, fd=fd, brs=brs, remote=remote):
                return rule
        raise JCanError("发送帧未匹配 profile 白名单")

    def allows_rx(self, frame: dict[str, Any]) -> bool:
        return any(
            rule.direction == "rx" and rule.matches(
                frame["id"], frame["data"], extended=frame["extended"], fd=frame["fd"],
                brs=frame["brs"], remote=frame["remote"],
            )
            for rule in self.frames
        )


class DeviceManager:
    """Serialize all libusb work onto one thread."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jcan-usb")

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, lambda: function(*args, **kwargs))

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


@dataclass
class PeriodicEntry:
    task_id: str
    serial: str
    rule: str
    can_id: int
    data: bytes
    fd: bool
    remote: bool
    brs: bool
    extended: bool
    period_ms: int
    target_count: int
    done: asyncio.Future[Any] = field(repr=False)
    state: str = "starting"
    next_deadline: float = 0.0
    sent_count: int = 0
    missed_periods: int = 0
    started_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at_utc: str | None = None
    error: str | None = None
    evidence_path: str | None = None
    timings: list[dict[str, float]] = field(default_factory=list, repr=False)

    def summary(self, include_timings: bool = False) -> dict[str, Any]:
        jitters = [sample["jitter_ms"] for sample in self.timings]
        result = {
            "task_id": self.task_id,
            "serial": self.serial,
            "rule": self.rule,
            "frame": {
                "id": self.can_id, "data": self.data.hex(" ").upper(), "fd": self.fd,
                "remote": self.remote, "brs": self.brs, "extended": self.extended,
            },
            "period_ms": self.period_ms,
            "target_count": self.target_count,
            "sent_count": self.sent_count,
            "missed_periods": self.missed_periods,
            "state": self.state,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "error": self.error,
            "evidence_path": self.evidence_path,
            "timing": {
                "samples": len(jitters),
                "jitter_ms": {
                    "min": min(jitters) if jitters else None,
                    "avg": sum(jitters) / len(jitters) if jitters else None,
                    "max": max(jitters) if jitters else None,
                },
                "last": self.timings[-1] if self.timings else None,
            },
        }
        if include_timings:
            result["timing_samples"] = self.timings
        return result


class PeriodicManager:
    """Run profile-approved periodic frames through one persistent CAN session."""

    def __init__(
        self, devices: DeviceManager, hardware_gate: asyncio.Lock, *,
        profile_path: Path | None = None, can_factory: Callable[[str], JCan] | None = None,
    ):
        self.devices = devices
        self.hardware_gate = hardware_gate
        self.profile_path = profile_path
        self.can_factory = can_factory or (lambda serial: JCan(LibUsb(), serial))
        self.active: dict[str, PeriodicEntry] = {}
        self.recent: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.runner: asyncio.Task[None] | None = None
        self.startup: asyncio.Future[None] | None = None
        self.profile: BusProfile | None = None
        self.closing = False
        self.cleanup_error: str | None = None
        self.next_id = 1

    def is_active(self, serial: str) -> bool:
        return any(entry.serial == serial for entry in self.active.values())

    async def start(
        self, serial: str, can_id: int, data_hex: str, period_ms: int, count: int, *,
        fd: bool, remote: bool, brs: bool, extended: bool,
    ) -> dict[str, Any]:
        profile = _load_profile(self.profile_path)
        profile.require_enabled(serial)
        data = _payload(data_hex)
        rule = profile.tx_rule(can_id, data, extended=extended, fd=fd, brs=brs, remote=remote)
        if not rule.periodic:
            raise JCanError(f"frame 规则 {rule.name} 未授权周期发送")
        if not isinstance(period_ms, int) or isinstance(period_ms, bool) or not profile.min_period_ms <= period_ms <= 60000:
            raise JCanError(f"period_ms 范围必须是 {profile.min_period_ms}..60000")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1500:
            raise JCanError("count 范围必须是 1..1500")

        while True:
            wait_for_runner = None
            async with self.lock:
                if self.closing and self.runner:
                    wait_for_runner = self.runner
                else:
                    if len(self.active) >= profile.max_periodic_tasks:
                        raise JCanError(f"周期任务数已达到 profile 上限 {profile.max_periodic_tasks}")
                    if self.profile is not None and self.profile != profile:
                        raise JCanError("运行中的周期会话与当前 profile 不一致")
                    loop = asyncio.get_running_loop()
                    task_id = f"periodic-{self.next_id}"
                    self.next_id += 1
                    entry = PeriodicEntry(
                        task_id, serial, rule.name, can_id, data, fd, remote, brs, extended,
                        period_ms, count, loop.create_future(),
                    )
                    if self.runner and not self.runner.done():
                        entry.state = "running"
                        entry.next_deadline = loop.time() + period_ms / 1000
                    self.active[task_id] = entry
                    self.wakeup.set()
                    if not self.runner or self.runner.done():
                        self.profile = profile
                        self.cleanup_error = None
                        self.startup = loop.create_future()
                        self.runner = loop.create_task(self._run(serial, profile, self.startup))
                    startup = self.startup
                    break
            if wait_for_runner:
                await wait_for_runner

        if startup:
            await startup
        return entry.summary()

    async def list(self, serial: str) -> dict[str, Any]:
        serial = _require_serial(serial)
        async with self.lock:
            return {
                "active": [entry.summary() for entry in self.active.values() if entry.serial == serial],
                "recent": [item for item in self.recent if item["serial"] == serial],
                "session_cleanup_error": self.cleanup_error,
            }

    async def stop(self, serial: str, task_id: str) -> dict[str, Any]:
        serial = _require_serial(serial)
        if not isinstance(task_id, str) or not task_id:
            raise JCanError("task_id 必须是非空字符串")
        async with self.lock:
            entry = self.active.get(task_id)
            if entry is None or entry.serial != serial:
                raise JCanError(f"未找到周期任务 {task_id}")
            entry.state = "stopping"
            self.wakeup.set()
            done = entry.done
        summary = await done
        if summary["state"] == "failed":
            raise RecoverableOperationError("周期任务失败", summary)
        async with self.lock:
            runner = self.runner if not self.active else None
        if runner:
            await runner
            if self.cleanup_error:
                raise JCanError(f"周期任务已停止，但会话清理失败: {self.cleanup_error}")
        return summary

    async def close(self) -> None:
        async with self.lock:
            for entry in self.active.values():
                entry.state = "stopping"
            self.wakeup.set()
            runner = self.runner
        if runner:
            await runner

    def _open(self, serial: str, profile: BusProfile) -> JCan:
        can = self.can_factory(serial)
        try:
            profile.verify_config(can)
            can.start(MODES["normal"])
            return can
        except Exception:
            can.close()
            raise

    def _finish_locked(self, entry: PeriodicEntry, state: str, error: Exception | None = None) -> dict[str, Any]:
        self.active.pop(entry.task_id, None)
        entry.state = state
        entry.error = str(error) if error else None
        entry.completed_at_utc = datetime.now(timezone.utc).isoformat()
        full = entry.summary(include_timings=True)
        result = _result("periodic_run", entry.serial, data=full, error=error)
        entry.evidence_path = result["evidence_path"]
        summary = entry.summary()
        self.recent.append(summary)
        self.recent[:] = self.recent[-16:]
        if not entry.done.done():
            entry.done.set_result(summary)
        return summary

    async def _run(self, serial: str, profile: BusProfile, startup: asyncio.Future[None]) -> None:
        can = None
        session_zero = 0.0
        failure = None
        try:
            async with self.hardware_gate:
                can = await self.devices.run(self._open, serial, profile)
                loop = asyncio.get_running_loop()
                session_zero = loop.time()
                async with self.lock:
                    for entry in self.active.values():
                        if entry.state != "stopping":
                            entry.state = "running"
                            entry.next_deadline = session_zero + entry.period_ms / 1000
                    if not startup.done():
                        startup.set_result(None)

                while True:
                    self.wakeup.clear()
                    async with self.lock:
                        for entry in list(self.active.values()):
                            if entry.state == "stopping":
                                self._finish_locked(entry, "stopped")
                        running = [entry for entry in self.active.values() if entry.state == "running"]
                        if not running:
                            self.closing = True
                            break
                        now = loop.time()
                        due = [entry for entry in running if entry.next_deadline <= now]
                        delay = max(0.0, min(entry.next_deadline for entry in running) - now)
                    if not due:
                        try:
                            await asyncio.wait_for(self.wakeup.wait(), delay)
                        except TimeoutError:
                            pass
                        continue

                    for entry in sorted(due, key=lambda item: item.next_deadline):
                        async with self.lock:
                            if self.active.get(entry.task_id) is not entry or entry.state not in {"running", "stopping"}:
                                continue
                            planned = entry.next_deadline
                        actual = loop.time()
                        await self.devices.run(
                            can.send, entry.can_id, entry.data, fd=entry.fd, remote=entry.remote,
                            brs=entry.brs, extended=entry.extended,
                        )
                        finished = loop.time()
                        async with self.lock:
                            if self.active.get(entry.task_id) is not entry:
                                continue
                            entry.sent_count += 1
                            entry.timings.append({
                                "planned_ms": (planned - session_zero) * 1000,
                                "actual_ms": (actual - session_zero) * 1000,
                                "jitter_ms": (actual - planned) * 1000,
                            })
                            if entry.state == "stopping":
                                self._finish_locked(entry, "stopped")
                            elif entry.sent_count >= entry.target_count:
                                self._finish_locked(entry, "completed")
                            else:
                                period = entry.period_ms / 1000
                                entry.next_deadline = planned + period
                                if entry.next_deadline <= finished:
                                    skipped = int((finished - entry.next_deadline) // period) + 1
                                    entry.missed_periods += skipped
                                    entry.next_deadline += skipped * period
        except Exception as exc:
            failure = exc
            async with self.lock:
                if not startup.done():
                    startup.set_exception(exc)
                for entry in list(self.active.values()):
                    self._finish_locked(entry, "failed", exc)
        finally:
            cleanup_errors = []
            if can is not None:
                try:
                    await self.devices.run(can.stop)
                except Exception as exc:
                    cleanup_errors.append(f"CANStop: {exc}")
                try:
                    await self.devices.run(can.close)
                except Exception as exc:
                    cleanup_errors.append(f"USB close: {exc}")
            self.cleanup_error = "; ".join(cleanup_errors) or None
            _result(
                "periodic_session", serial,
                data={"cleanup_state": "stopped", "cleanup_errors": cleanup_errors},
                error=JCanError(self.cleanup_error) if self.cleanup_error else None,
            )
            async with self.lock:
                if not startup.done():
                    startup.set_exception(failure or JCanError("周期会话未启动"))
                self.runner = None
                self.startup = None
                self.profile = None
                self.closing = False
                self.wakeup.set()


@dataclass
class AppContext:
    devices: DeviceManager
    hardware_gate: asyncio.Lock
    periodic: PeriodicManager


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    devices = DeviceManager()
    hardware_gate = asyncio.Lock()
    periodic = PeriodicManager(devices, hardware_gate)
    try:
        yield AppContext(devices, hardware_gate, periodic)
    finally:
        await periodic.close()
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


def _load_profile(path: Path | None = None) -> BusProfile:
    return BusProfile.load(path or PROFILE_PATH)


def _profile_status(path: Path | None = None) -> dict[str, Any]:
    profile = _load_profile(path)
    return {
        "path": str(profile.path.resolve()),
        "name": profile.name,
        "enabled": profile.enabled,
        "approved": profile.approved,
        "authorization": profile.authorization,
        "approved_at_utc": profile.approved_at_utc,
        "serial": profile.serial,
        "nominal_bitrate": profile.nominal_bitrate,
        "data_bitrate": profile.data_bitrate,
        "fd_standard": profile.fd_standard,
        "min_period_ms": profile.min_period_ms,
        "max_periodic_tasks": profile.max_periodic_tasks,
        "cleanup_state": profile.cleanup_state,
        "max_capture_ms": profile.max_capture_ms,
        "max_capture_frames": profile.max_capture_frames,
        "frames": [
            {
                "name": rule.name,
                "direction": rule.direction,
                "id": rule.can_id,
                "extended": rule.extended,
                "fd": rule.fd,
                "brs": rule.brs,
                "remote": rule.remote,
                "periodic": rule.periodic,
                "dlc": sorted(rule.dlc),
                "payload_variants": len(rule.payloads),
            }
            for rule in profile.frames
        ],
    }


def _payload(data_hex: str) -> bytes:
    if not isinstance(data_hex, str):
        raise JCanError("data_hex 必须是十六进制字符串")
    try:
        return bytes.fromhex(data_hex)
    except ValueError as exc:
        raise JCanError("data_hex 不是有效十六进制") from exc


def _frame_summary(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": frame["id"],
        "data": frame["data"].hex(" ").upper(),
        "fd": frame["fd"],
        "remote": frame["remote"],
        "brs": frame["brs"],
        "extended": frame["extended"],
        "timestamp": frame["timestamp"],
    }


def _sdo_target(node: int, index: int, subindex: int) -> None:
    if not isinstance(node, int) or isinstance(node, bool) or not 1 <= node <= 0x7F:
        raise JCanError("CANopen Node-ID 范围必须是 1..127")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 0xFFFF:
        raise JCanError("SDO index 范围必须是 0..65535")
    if not isinstance(subindex, int) or isinstance(subindex, bool) or not 0 <= subindex <= 0xFF:
        raise JCanError("SDO subindex 范围必须是 0..255")


def _sdo_rx_rule(profile: BusProfile, node: int, data: bytes) -> FrameRule:
    frame = {
        "id": 0x580 + node, "data": data, "fd": False, "remote": False,
        "brs": False, "extended": False,
    }
    for rule in profile.frames:
        if rule.direction == "rx" and rule.matches(
            frame["id"], data, extended=False, fd=False, brs=False, remote=False,
        ):
            return rule
    raise JCanError("SDO 响应未匹配 profile RX 白名单")


def _sdo_read(
    serial: str, node: int, index: int, subindex: int, *, profile_path: Path | None = None,
    can_factory: Callable[[str], JCan] | None = None,
) -> dict[str, Any]:
    _sdo_target(node, index, subindex)
    profile = _load_profile(profile_path)
    profile.require_enabled(serial)
    request = struct.pack("<BHB4x", 0x40, index, subindex)
    tx_rule = profile.tx_rule(0x600 + node, request, extended=False, fd=False, brs=False, remote=False)
    factory = can_factory or (lambda selected: JCan(LibUsb(), selected))
    parser = CanStreamParser()
    with factory(serial) as can:
        profile.verify_config(can)
        can.enable_receive()
        can.start(MODES["normal"])
        try:
            value, response = sdo_read(can, parser, node, index, subindex)
            rx_rule = _sdo_rx_rule(profile, node, response)
        finally:
            can.stop()
    return {
        "profile": profile.name, "node": node, "index": index, "subindex": subindex,
        "value_hex": value.hex(" ").upper(), "value_unsigned": int.from_bytes(value, "little"),
        "request": request.hex(" ").upper(), "response": response.hex(" ").upper(),
        "tx_rule": tx_rule.name, "rx_rule": rx_rule.name,
    }


def _sdo_u16_same_value_test(
    serial: str, node: int, index: int, subindex: int, *, profile_path: Path | None = None,
    can_factory: Callable[[str], JCan] | None = None,
) -> dict[str, Any]:
    _sdo_target(node, index, subindex)
    profile = _load_profile(profile_path)
    profile.require_enabled(serial)
    upload = struct.pack("<BHB4x", 0x40, index, subindex)
    upload_rule = profile.tx_rule(0x600 + node, upload, extended=False, fd=False, brs=False, remote=False)
    factory = can_factory or (lambda selected: JCan(LibUsb(), selected))
    parser = CanStreamParser()
    with factory(serial) as can:
        profile.verify_config(can)
        can.enable_receive()
        can.start(MODES["normal"])
        try:
            baseline, first_upload = sdo_read(can, parser, node, index, subindex)
            first_rx_rule = _sdo_rx_rule(profile, node, first_upload)
            if len(baseline) != 2:
                raise JCanError(f"SDO 对象不是 U16: 返回 {len(baseline)} 字节")
            download = struct.pack("<BHB", 0x2B, index, subindex) + baseline + b"\0\0"
            download_rule = profile.tx_rule(
                0x600 + node, download, extended=False, fd=False, brs=False, remote=False,
            )
            download_response = sdo_write(can, parser, node, index, subindex, baseline)
            download_rx_rule = _sdo_rx_rule(profile, node, download_response)
            verified, second_upload = sdo_read(can, parser, node, index, subindex)
            second_rx_rule = _sdo_rx_rule(profile, node, second_upload)
            if verified != baseline:
                raise JCanError("SDO 同值写入后回读不匹配")
        finally:
            can.stop()
    return {
        "profile": profile.name, "node": node, "index": index, "subindex": subindex,
        "value_hex": baseline.hex(" ").upper(), "value_unsigned": int.from_bytes(baseline, "little"),
        "write_changed_value": False, "eeprom_save": False,
        "requests": [upload.hex(" ").upper(), download.hex(" ").upper(), upload.hex(" ").upper()],
        "responses": [first_upload.hex(" ").upper(), download_response.hex(" ").upper(), second_upload.hex(" ").upper()],
        "rules": [upload_rule.name, first_rx_rule.name, download_rule.name, download_rx_rule.name, second_rx_rule.name],
    }


def _send_once(
    serial: str, can_id: int, data_hex: str, *, fd: bool, remote: bool, brs: bool, extended: bool,
    profile_path: Path | None = None, can_factory: Callable[[str], JCan] | None = None,
) -> dict[str, Any]:
    profile = _load_profile(profile_path)
    profile.require_enabled(serial)
    data = _payload(data_hex)
    rule = profile.tx_rule(can_id, data, extended=extended, fd=fd, brs=brs, remote=remote)
    factory = can_factory or (lambda selected: JCan(LibUsb(), selected))
    with factory(serial) as can:
        profile.verify_config(can)
        can.start(MODES["normal"])
        try:
            can.send(can_id, data, fd=fd, remote=remote, brs=brs, extended=extended)
        finally:
            can.stop()
    return {
        "profile": profile.name,
        "rule": rule.name,
        "nominal_bitrate": profile.nominal_bitrate,
        "data_bitrate": profile.data_bitrate,
        "frame": {"id": can_id, "data": data.hex(" ").upper(), "fd": fd, "remote": remote, "brs": brs, "extended": extended},
    }


def _capture(
    serial: str, duration_ms: int, max_frames: int, *, profile_path: Path | None = None,
    can_factory: Callable[[str], JCan] | None = None,
) -> dict[str, Any]:
    profile = _load_profile(profile_path)
    profile.require_enabled(serial)
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or not 1 <= duration_ms <= profile.max_capture_ms:
        raise JCanError(f"duration_ms 范围必须是 1..{profile.max_capture_ms}")
    if not isinstance(max_frames, int) or isinstance(max_frames, bool) or not 1 <= max_frames <= profile.max_capture_frames:
        raise JCanError(f"max_frames 范围必须是 1..{profile.max_capture_frames}")
    if not any(rule.direction == "rx" for rule in profile.frames):
        raise JCanError("profile 未定义 RX frame 规则")

    factory = can_factory or (lambda selected: JCan(LibUsb(), selected))
    parser = CanStreamParser()
    samples = []
    received = matched = 0
    started = time.monotonic()
    deadline = started + duration_ms / 1000
    with factory(serial) as can:
        profile.verify_config(can)
        can.enable_receive()
        can.start(MODES["silent"])
        try:
            while received < max_frames and time.monotonic() < deadline:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                for frame in parser.feed(can.receive(min(100, remaining_ms))):
                    received += 1
                    if profile.allows_rx(frame):
                        matched += 1
                        if len(samples) < 50:
                            samples.append(_frame_summary(frame))
                    if received >= max_frames:
                        break
        finally:
            can.stop()
    return {
        "profile": profile.name,
        "duration_ms": (time.monotonic() - started) * 1000,
        "received_frames": received,
        "matched_frames": matched,
        "dropped_frames": received - matched,
        "samples": samples,
    }


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


async def _hardware_call(
    ctx: Context, operation: str, serial: str | None, function: Callable[..., Any], *args: Any, **kwargs: Any,
) -> dict[str, Any]:
    try:
        app = ctx.request_context.lifespan_context
        if serial and app.periodic.is_active(serial):
            raise JCanError(f"适配器 {serial} 正在执行周期任务")
        async with app.hardware_gate:
            if serial and app.periodic.is_active(serial):
                raise JCanError(f"适配器 {serial} 正在执行周期任务")
            data = await app.devices.run(function, *args, **kwargs)
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
async def jcan_bus_profile_status() -> dict[str, Any]:
    """Read and validate the local physical-bus profile without accessing USB."""
    try:
        return _result("bus_profile_status", None, data=_profile_status())
    except (JCanError, OSError, ValueError) as exc:
        return _result("bus_profile_status", None, error=exc)


@mcp.tool()
async def jcan_capture(serial: str, ctx: Context, duration_ms: int = 1000, max_frames: int = 100) -> dict[str, Any]:
    """Capture bounded listen-only traffic allowed by the active physical-bus profile."""
    return await _hardware_call(ctx, "capture", serial, _capture, serial, duration_ms, max_frames)


@mcp.tool()
async def jcan_send_once(
    serial: str, can_id: int, data_hex: str, ctx: Context, fd: bool = False, remote: bool = False,
    brs: bool = False, extended: bool = False,
) -> dict[str, Any]:
    """Send one frame only when serial, flags, DLC, and exact payload match the active profile."""
    return await _hardware_call(
        ctx, "send_once", serial, _send_once, serial, can_id, data_hex,
        fd=fd, remote=remote, brs=brs, extended=extended,
    )


@mcp.tool()
async def jcan_periodic_start(
    serial: str, can_id: int, data_hex: str, period_ms: int, count: int, ctx: Context,
    fd: bool = False, remote: bool = False, brs: bool = False, extended: bool = False,
) -> dict[str, Any]:
    """Start a bounded profile-approved periodic task; count must be 1..1500."""
    try:
        data = await ctx.request_context.lifespan_context.periodic.start(
            serial, can_id, data_hex, period_ms, count,
            fd=fd, remote=remote, brs=brs, extended=extended,
        )
        return _result("periodic_start", serial, data=data)
    except (JCanError, OSError, ValueError) as exc:
        return _result("periodic_start", serial, error=exc)


@mcp.tool()
async def jcan_periodic_list(serial: str, ctx: Context) -> dict[str, Any]:
    """List active and recent bounded periodic tasks for one adapter serial."""
    try:
        data = await ctx.request_context.lifespan_context.periodic.list(serial)
        return _result("periodic_list", serial, data=data)
    except (JCanError, OSError, ValueError) as exc:
        return _result("periodic_list", serial, error=exc)


@mcp.tool()
async def jcan_periodic_stop(serial: str, task_id: str, ctx: Context) -> dict[str, Any]:
    """Stop one periodic task and wait until an in-flight send and cleanup are complete."""
    try:
        data = await ctx.request_context.lifespan_context.periodic.stop(serial, task_id)
        return _result("periodic_stop", serial, data=data)
    except (JCanError, OSError, ValueError) as exc:
        return _result("periodic_stop", serial, error=exc)


@mcp.tool()
async def jcan_sdo_read(
    serial: str, node: int, index: int, subindex: int, ctx: Context,
) -> dict[str, Any]:
    """Read one expedited CANopen SDO object allowed by the active physical-bus profile."""
    return await _hardware_call(ctx, "sdo_read", serial, _sdo_read, serial, node, index, subindex)


@mcp.tool()
async def jcan_sdo_u16_same_value_test(
    serial: str, node: int, index: int, subindex: int, ctx: Context,
) -> dict[str, Any]:
    """Read a profile-approved U16 object, download the same value, and verify it by upload."""
    return await _hardware_call(
        ctx, "sdo_u16_same_value_test", serial, _sdo_u16_same_value_test, serial, node, index, subindex,
    )


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
