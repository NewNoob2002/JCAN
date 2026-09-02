#!/usr/bin/env python3
"""Minimal native Linux CLI for Jooiee JTool-CAN (ffff:0004)."""

import argparse
import ctypes
import struct
import sys
import time

VID = 0xFFFF
PID = 0x0004
EP_COMMAND_OUT = 0x02
EP_COMMAND_IN = 0x82
EP_STREAM_IN = 0x83
HEADER = struct.Struct("<BBBBH")
DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
MODES = {"normal": 0, "silent": 1, "loopback": 2, "silent-loopback": 3}
CONFIG_VALUE_OFFSET = 16
CONFIG_FIELDS = (
    ("can_speed", 1), ("can_customval", 10),
    ("fd_speed", 1), ("fd_customval", 10),
    ("standard", 1), ("term_res", 1),
    ("busoff_recovery", 1), ("auto_retrans", 1),
    ("hardware_version", 10), ("id", 2),
)


class JCanError(RuntimeError):
    pass


class UsbDeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class LibUsb:
    def __init__(self):
        try:
            self.lib = ctypes.CDLL("libusb-1.0.so.0")
        except OSError as exc:
            raise JCanError("缺少 libusb-1.0.so.0，请安装 libusb-1.0 运行库") from exc
        self._declare()
        self.ctx = ctypes.c_void_p()
        self._check(self.lib.libusb_init(ctypes.byref(self.ctx)), "初始化 libusb")

    def _declare(self):
        lib = self.lib
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_exit.argtypes = [ctypes.c_void_p]
        lib.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
        lib.libusb_get_device_list.restype = ctypes.c_ssize_t
        lib.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        lib.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(UsbDeviceDescriptor)]
        lib.libusb_get_device_descriptor.restype = ctypes.c_int
        lib.libusb_get_bus_number.argtypes = [ctypes.c_void_p]
        lib.libusb_get_bus_number.restype = ctypes.c_uint8
        lib.libusb_get_device_address.argtypes = [ctypes.c_void_p]
        lib.libusb_get_device_address.restype = ctypes.c_uint8
        lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_open.restype = ctypes.c_int
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_get_string_descriptor_ascii.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
        lib.libusb_get_string_descriptor_ascii.restype = ctypes.c_int
        lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int
        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int
        lib.libusb_bulk_transfer.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
        lib.libusb_bulk_transfer.restype = ctypes.c_int
        lib.libusb_error_name.argtypes = [ctypes.c_int]
        lib.libusb_error_name.restype = ctypes.c_char_p

    def _check(self, rc, action):
        if rc < 0:
            name = self.lib.libusb_error_name(rc).decode(errors="replace")
            hint = "；请安装下面给出的 udev 规则后重新插拔设备" if rc == -3 else ""
            raise JCanError(f"{action}失败: {name} ({rc}){hint}")
        return rc

    def devices(self):
        items = ctypes.POINTER(ctypes.c_void_p)()
        count = self._check(self.lib.libusb_get_device_list(self.ctx, ctypes.byref(items)), "枚举 USB 设备")
        try:
            for index in range(count):
                dev = items[index]
                desc = UsbDeviceDescriptor()
                if self.lib.libusb_get_device_descriptor(dev, ctypes.byref(desc)) == 0 and desc.idVendor == VID and desc.idProduct == PID:
                    yield dev, desc, self.lib.libusb_get_bus_number(dev), self.lib.libusb_get_device_address(dev)
        finally:
            self.lib.libusb_free_device_list(items, 1)

    def serial(self, dev, desc):
        handle = ctypes.c_void_p()
        if self.lib.libusb_open(dev, ctypes.byref(handle)) < 0:
            return None
        try:
            if not desc.iSerialNumber:
                return ""
            buf = (ctypes.c_ubyte * 256)()
            size = self.lib.libusb_get_string_descriptor_ascii(handle, desc.iSerialNumber, buf, len(buf))
            return bytes(buf[:size]).decode(errors="replace") if size >= 0 else None
        finally:
            self.lib.libusb_close(handle)

    def open(self, wanted_serial=None):
        matches = []
        for dev, desc, bus, address in self.devices():
            handle = ctypes.c_void_p()
            rc = self.lib.libusb_open(dev, ctypes.byref(handle))
            if rc < 0:
                matches.append((bus, address, rc))
                continue
            serial = ""
            if desc.iSerialNumber:
                buf = (ctypes.c_ubyte * 256)()
                size = self.lib.libusb_get_string_descriptor_ascii(handle, desc.iSerialNumber, buf, len(buf))
                serial = bytes(buf[:size]).decode(errors="replace") if size >= 0 else ""
            if wanted_serial and serial != wanted_serial:
                self.lib.libusb_close(handle)
                continue
            self.lib.libusb_set_auto_detach_kernel_driver(handle, 0)
            try:
                self._check(self.lib.libusb_claim_interface(handle, 0), "占用 JTool-CAN USB 接口")
            except Exception:
                self.lib.libusb_close(handle)
                raise
            return handle, serial, bus, address
        if matches:
            self._check(matches[0][2], "打开 JTool-CAN")
        target = f"（序列号 {wanted_serial}）" if wanted_serial else ""
        raise JCanError(f"未找到 JTool-CAN {target}")

    def close(self, handle):
        self.lib.libusb_release_interface(handle, 0)
        self.lib.libusb_close(handle)

    def transfer_out(self, handle, endpoint, data, timeout_ms=4500):
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        transferred = ctypes.c_int()
        self._check(self.lib.libusb_bulk_transfer(handle, endpoint, buf, len(data), ctypes.byref(transferred), timeout_ms), "USB 写入")
        if transferred.value != len(data):
            raise JCanError(f"USB 短写: {transferred.value}/{len(data)}")

    def transfer_in(self, handle, endpoint, size=4096, timeout_ms=4500, allow_timeout=False):
        buf = (ctypes.c_ubyte * size)()
        transferred = ctypes.c_int()
        rc = self.lib.libusb_bulk_transfer(handle, endpoint, buf, size, ctypes.byref(transferred), timeout_ms)
        if allow_timeout and rc == -7:
            return b""
        self._check(rc, "USB 读取")
        return bytes(buf[:transferred.value])

    def __del__(self):
        if getattr(self, "ctx", None):
            self.lib.libusb_exit(self.ctx)
            self.ctx = None


def packet(channel, command, payload=b"", response=0):
    return HEADER.pack(0x4A, channel, response, command, len(payload)) + payload


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ 0x07) & 0xFF if value & 0x80 else (value << 1) & 0xFF
    return value


class CanStreamParser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xaa")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b"\xff" else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 3:
                break
            flags = self.buffer[2]
            length = 0 if flags & 0x20 else DLC_LENGTHS[flags & 0x0F]
            total = 10 + length
            if len(self.buffer) < total:
                break
            raw = bytes(self.buffer[:total])
            del self.buffer[:total]
            if crc8(raw[:-1]) != raw[-1]:
                continue
            frames.append({
                "fd": bool(flags & 0x10),
                "remote": bool(flags & 0x20),
                "brs": bool(flags & 0x40),
                "extended": bool(flags & 0x80),
                "id": struct.unpack_from("<I", raw, 3)[0],
                "timestamp": struct.unpack_from("<H", raw, 7)[0],
                "data": raw[9:-1],
            })
        return frames


class JCan:
    def __init__(self, usb, serial=None):
        self.usb = usb
        self.handle, self.serial, self.bus, self.address = usb.open(serial)

    def close(self):
        if self.handle:
            self.usb.close(self.handle)
            self.handle = None

    def request(self, channel, command, payload=b""):
        self.usb.transfer_out(self.handle, EP_COMMAND_OUT, packet(channel, command, payload))
        reply = self.usb.transfer_in(self.handle, EP_COMMAND_IN)
        if len(reply) < HEADER.size:
            raise JCanError("设备回复过短")
        magic, _, response, reply_command, length = HEADER.unpack_from(reply)
        body = reply[HEADER.size:]
        if magic != 0x4A or response != 1 or reply_command != command or len(body) != length:
            raise JCanError(f"设备回复格式错误: {reply.hex(' ')}")
        if not body or body[0] != 1:
            raise JCanError(f"设备拒绝命令: {body.hex(' ')}")
        return body[1:]

    def start(self, mode=0):
        self.request(5, 1, bytes([mode]))

    def stop(self):
        self.request(5, 2)

    def send(self, can_id, data, *, fd=False, remote=False, brs=False, extended=False):
        if can_id < 0 or can_id > (0x1FFFFFFF if extended else 0x7FF):
            raise JCanError("CAN ID 超出标准帧/扩展帧范围")
        if remote and (fd or data):
            raise JCanError("远程帧只支持经典 CAN 且不能携带数据")
        if brs and not fd:
            raise JCanError("BRS 只支持 CAN FD")
        allowed = DLC_LENGTHS if fd else range(9)
        if len(data) not in allowed:
            raise JCanError("经典 CAN 长度需为 0..8；CAN FD 长度需为 0..8/12/16/20/24/32/48/64")
        payload = struct.pack("<BBBBIB", fd, remote, brs, extended, can_id, len(data)) + data.ljust(64, b"\0")
        self.request(5, 0, payload)

    @staticmethod
    def _config_key(name):
        return name.encode("ascii") + b"\0"

    def _config_get(self, name, size):
        body = self.request(0, 1, self._config_key(name))
        offset = CONFIG_VALUE_OFFSET - 1  # request() removes the leading success byte
        if len(body) < offset + size or struct.unpack_from("<H", body, 4)[0] != size:
            raise JCanError(f"配置 {name} 回复格式错误: {body.hex(' ')}")
        return body[offset:offset + size]

    def _config_set(self, name, value):
        self.request(0, 2, self._config_key(name) + value)

    @staticmethod
    def _uint(value, name, maximum):
        if not isinstance(value, int) or not 0 <= value <= maximum:
            raise JCanError(f"{name} 范围必须是 0..{maximum}")
        return value

    def set_nominal_speed(self, value):
        value = self._uint(value, "速率编号", 0xFF)
        custom = bytearray(self._config_get("can_customval", 10))
        self._config_set("can_speed", bytes([value]))
        custom[:2] = b"\0\0"
        self._config_set("can_customval", custom)

    def set_data_speed(self, value):
        value = self._uint(value, "速率编号", 0xFF)
        custom = bytearray(self._config_get("fd_customval", 10))
        self._config_set("fd_speed", bytes([value]))
        custom[:2] = b"\0\0"
        self._config_set("fd_customval", custom)

    def set_nominal_custom_speed(self, prescaler, sjw, seg1, seg2):
        if not (1 <= prescaler <= 512 and 1 <= sjw <= 128 and 2 <= seg1 <= 256 and 2 <= seg2 <= 128 and sjw <= seg2):
            raise JCanError("经典 CAN 自定义时序范围: prescaler 1..512, sjw 1..128, seg1 2..256, seg2 2..128, sjw <= seg2")
        self._config_set("can_customval", struct.pack("<5H", 1, prescaler, sjw, seg1, seg2))

    def set_data_custom_speed(self, prescaler, sjw, seg1, seg2):
        if not (1 <= prescaler <= 32 and 1 <= sjw <= 16 and 1 <= seg1 <= 32 and 1 <= seg2 <= 16 and sjw <= seg2):
            raise JCanError("CAN FD 数据相位时序范围: prescaler 1..32, sjw 1..16, seg1 1..32, seg2 1..16, sjw <= seg2")
        self._config_set("fd_customval", struct.pack("<5H", 1, prescaler, sjw, seg1, seg2))

    def set_fd_standard(self, standard):
        standard = self._uint(standard, "CAN FD 标准编号", 1)
        self._config_set("standard", bytes([standard]))

    def set_terminal_resistance(self, enabled):
        enabled = self._uint(enabled, "终端电阻开关", 1)
        self._config_set("term_res", bytes([enabled]))

    def set_busoff_auto_recovery(self, enabled):
        enabled = self._uint(enabled, "Bus-Off 自动恢复开关", 1)
        self._config_set("busoff_recovery", bytes([enabled]))

    def set_auto_retransmission(self, enabled):
        enabled = self._uint(enabled, "自动重传开关", 1)
        self._config_set("auto_retrans", bytes([enabled]))

    def reboot(self):
        self.request(0, 3)

    def set_hardware_version(self, version):
        try:
            value = version.encode("ascii")
        except UnicodeEncodeError as exc:
            raise JCanError("硬件版本必须是 ASCII 字符") from exc
        if len(value) > 9:
            raise JCanError("硬件版本最多 9 个 ASCII 字符")
        self._config_set("hardware_version", value.ljust(10, b"\0"))

    def set_device_id(self, value):
        value = self._uint(value, "设备 ID", 0xFFFF)
        self._config_set("id", struct.pack("<H", value))

    def into_boot(self):
        self._config_set("stop_in_boot", b"\x01")
        self.reboot()

    def enable_receive(self):
        self.request(0, 4)

    def receive(self, timeout_ms=1000):
        outer = self.usb.transfer_in(self.handle, EP_STREAM_IN, timeout_ms=timeout_ms, allow_timeout=True)
        if not outer:
            return b""
        if len(outer) < HEADER.size:
            raise JCanError("接收数据包过短")
        magic, channel, response, command, length = HEADER.unpack_from(outer)
        body = outer[HEADER.size:]
        if magic != 0x4A or channel != 5 or response != 1 or command != 0xFF or len(body) != length:
            raise JCanError(f"接收数据包格式错误: {outer.hex(' ')}")
        return body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def parse_hex(text, name, maximum):
    try:
        value = int(text.removeprefix("0x"), 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} 必须是十六进制数") from exc
    if not 0 <= value <= maximum:
        raise argparse.ArgumentTypeError(f"{name} 范围必须是 0..{maximum:X}")
    return value


def parse_int(text, name, minimum, maximum):
    try:
        value = int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise argparse.ArgumentTypeError(f"{name} 范围必须是 {minimum}..{maximum}")
    return value


def print_frame(frame):
    flags = []
    if frame["extended"]:
        flags.append("EXT")
    if frame["fd"]:
        flags.append("FD")
    if frame["brs"]:
        flags.append("BRS")
    if frame["remote"]:
        flags.append("RTR")
    width = 8 if frame["extended"] else 3
    data = " ".join(f"{byte:02X}" for byte in frame["data"])
    print(f"{frame['timestamp']:05d}  {frame['id']:0{width}X}  [{len(frame['data'])}] {data}  {'|'.join(flags)}".rstrip())


def wait_frame(can, parser, can_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in parser.feed(can.receive(min(500, max(1, int((deadline - time.monotonic()) * 1000))))):
            if frame["id"] == can_id:
                return frame
    raise JCanError(f"等待 CAN ID {can_id:03X} 超时")


def run_loopback_test(can):
    parser = CanStreamParser()
    tests = [
        (0x123, bytes.fromhex("11 22 33 44"), {}),
        (0x18FF0011, bytes.fromhex("00 01 02 03 04 05 06 07"), {"extended": True}),
        (0x321, bytes(range(12)), {"fd": True, "brs": True}),
    ]
    frames = []
    can.enable_receive()
    can.start(MODES["silent-loopback"])
    try:
        for can_id, expected, options in tests:
            can.send(can_id, expected, **options)
            frame = wait_frame(can, parser, can_id)
            if frame["data"] != expected or any(frame[key] != value for key, value in options.items()):
                raise JCanError(f"loopback 数据或标志不匹配: {frame}")
            frames.append(frame)
    finally:
        can.stop()
    return frames


def loopback_benchmark(can, count):
    parser = CanStreamParser()
    latencies = []
    can.enable_receive()
    can.start(MODES["silent-loopback"])
    started = time.monotonic_ns()
    try:
        for sequence in range(count):
            payload = struct.pack("<II", sequence, ~sequence & 0xFFFFFFFF)
            sent = time.monotonic_ns()
            can.send(0x555, payload)
            frame = wait_frame(can, parser, 0x555)
            if frame["data"] != payload:
                raise JCanError(f"基准帧数据不匹配: {frame['data'].hex(' ')}")
            latencies.append((time.monotonic_ns() - sent) / 1_000_000)
    finally:
        can.stop()
    elapsed = (time.monotonic_ns() - started) / 1_000_000_000
    ordered = sorted(latencies)
    p95 = ordered[(95 * len(ordered) - 1) // 100]
    return {
        "count": count,
        "elapsed_s": elapsed,
        "fps": count / elapsed,
        "latency_ms": {
            "min": ordered[0],
            "avg": sum(ordered) / count,
            "p95": p95,
            "max": ordered[-1],
        },
    }


def parse_sdo_upload(data, index, subindex):
    if len(data) != 8 or data[1:4] != struct.pack("<HB", index, subindex):
        raise JCanError("SDO 回复格式或索引不匹配")
    if data[0] == 0x80:
        raise JCanError(f"SDO abort 0x{int.from_bytes(data[4:8], 'little'):08X}")
    if data[0] & 0xE0 != 0x40 or not data[0] & 0x02:
        raise JCanError("暂不支持分段 SDO upload")
    size = 4 - ((data[0] >> 2) & 3) if data[0] & 1 else 4
    return data[4:4 + size]


def parse_sdo_download(data, index, subindex):
    if len(data) != 8 or data[1:4] != struct.pack("<HB", index, subindex):
        raise JCanError("SDO 回复格式或索引不匹配")
    if data[0] == 0x80:
        raise JCanError(f"SDO abort 0x{int.from_bytes(data[4:8], 'little'):08X}")
    if data[0] != 0x60:
        raise JCanError("SDO download 回复格式错误")


def sdo_read(can, parser, node, index, subindex):
    can.send(0x600 + node, struct.pack("<BHB4x", 0x40, index, subindex))
    while True:
        frame = wait_frame(can, parser, 0x580 + node)
        if not any(frame[flag] for flag in ("extended", "fd", "remote", "brs")) and len(frame["data"]) == 8 and frame["data"][1:4] == struct.pack("<HB", index, subindex):
            return parse_sdo_upload(frame["data"], index, subindex), frame["data"]


def sdo_write(can, parser, node, index, subindex, value):
    commands = {1: 0x2F, 2: 0x2B, 3: 0x27, 4: 0x23}
    if len(value) not in commands:
        raise JCanError("仅支持 1..4 字节 expedited SDO download")
    can.send(0x600 + node, struct.pack("<BHB", commands[len(value)], index, subindex) + value.ljust(4, b"\0"))
    while True:
        frame = wait_frame(can, parser, 0x580 + node)
        if not any(frame[flag] for flag in ("extended", "fd", "remote", "brs")) and len(frame["data"]) == 8 and frame["data"][1:4] == struct.pack("<HB", index, subindex):
            parse_sdo_download(frame["data"], index, subindex)
            return frame["data"]


def self_test(verbose=True):
    assert packet(5, 1, b"\x00") == b"J\x05\x00\x01\x01\x00\x00"
    flags = 0x80 | 3
    raw = b"\xff\xaa" + bytes([flags]) + struct.pack("<IH", 0x18FF0011, 42) + b"\x11\x22\x33"
    raw += bytes([crc8(raw)])
    parser = CanStreamParser()
    assert parser.feed(raw[:4]) == []
    frame = parser.feed(raw[4:])[0]
    assert frame["extended"] and frame["id"] == 0x18FF0011 and frame["data"] == b"\x11\x22\x33"
    assert parse_sdo_upload(bytes.fromhex("43 18 10 01 78 56 34 12"), 0x1018, 1) == bytes.fromhex("78 56 34 12")
    assert parse_sdo_download(bytes.fromhex("60 08 20 00 00 00 00 00"), 0x2008, 0) is None
    try:
        parse_sdo_download(bytes.fromhex("80 08 20 00 00 00 02 06"), 0x2008, 0)
        assert False, "SDO abort 应抛出异常"
    except JCanError:
        pass

    class FakeUsb:
        def __init__(self, replies):
            self.replies = list(replies)
            self.sent = []

        def open(self, _serial):
            return object(), "TEST", 1, 2

        def close(self, _handle):
            pass

        def transfer_out(self, _handle, endpoint, data, timeout_ms=4500):
            assert endpoint == EP_COMMAND_OUT and timeout_ms == 4500
            self.sent.append(data)

        def transfer_in(self, _handle, endpoint, size=4096, timeout_ms=4500, allow_timeout=False):
            assert endpoint == EP_COMMAND_IN and size == 4096 and timeout_ms == 4500 and not allow_timeout
            return self.replies.pop(0)

    def ok(command, body=b"\x01"):
        return packet(0, command, body, response=1)

    def check(method, args, expected, replies=None):
        usb = FakeUsb(replies or [ok(2)] * len(expected))
        with JCan(usb) as can:
            getattr(can, method)(*args)
        assert usb.sent == expected and not usb.replies

    def config_reply(value):
        body = bytearray(CONFIG_VALUE_OFFSET + len(value))
        body[0] = 1
        struct.pack_into("<H", body, 5, len(value))
        body[CONFIG_VALUE_OFFSET:] = value
        return ok(1, body)

    custom = bytes.fromhex("01 00 02 00 03 00 04 00 05 00")
    check("set_nominal_speed", (7,), [
        packet(0, 1, b"can_customval\0"),
        packet(0, 2, b"can_speed\0\x07"),
        packet(0, 2, b"can_customval\0\0\0" + custom[2:]),
    ], [config_reply(custom), ok(2), ok(2)])
    check("set_data_speed", (3,), [
        packet(0, 1, b"fd_customval\0"),
        packet(0, 2, b"fd_speed\0\x03"),
        packet(0, 2, b"fd_customval\0\0\0" + custom[2:]),
    ], [config_reply(custom), ok(2), ok(2)])
    check("set_nominal_custom_speed", (512, 128, 256, 128), [packet(0, 2, b"can_customval\0" + struct.pack("<5H", 1, 512, 128, 256, 128))])
    check("set_data_custom_speed", (32, 16, 32, 16), [packet(0, 2, b"fd_customval\0" + struct.pack("<5H", 1, 32, 16, 32, 16))])
    for method, args in [("set_nominal_custom_speed", (1, 3, 2, 2)), ("set_data_custom_speed", (1, 2, 1, 1))]:
        try:
            check(method, args, [])
            assert False, f"{method} 应拒绝 sjw > seg2"
        except JCanError:
            pass
    for method, args, key, value in [
        ("set_fd_standard", (1,), "standard", b"\x01"),
        ("set_terminal_resistance", (1,), "term_res", b"\x01"),
        ("set_busoff_auto_recovery", (0,), "busoff_recovery", b"\x00"),
        ("set_auto_retransmission", (1,), "auto_retrans", b"\x01"),
        ("set_hardware_version", ("HW1",), "hardware_version", b"HW1".ljust(10, b"\0")),
        ("set_device_id", (0x1234,), "id", b"\x34\x12"),
    ]:
        check(method, args, [packet(0, 2, key.encode() + b"\0" + value)])
    try:
        check("set_terminal_resistance", (2,), [])
        assert False, "set_terminal_resistance 应拒绝非 0/1 参数"
    except JCanError:
        pass
    check("reboot", (), [packet(0, 3)], [ok(3)])
    check("into_boot", (), [packet(0, 2, b"stop_in_boot\0\x01"), packet(0, 3)], [ok(2), ok(3)])
    if verbose:
        print("self-test: OK")


def config_roundtrip_test(can, verbose=True):
    baseline = {name: can._config_get(name, size) for name, size in CONFIG_FIELDS}

    def verify(name, expected):
        actual = can._config_get(name, len(expected))
        if actual != expected:
            raise JCanError(f"配置 {name} 回读不匹配: {actual.hex(' ')} != {expected.hex(' ')}")
        if verbose:
            print(f"{name}: OK")

    try:
        _, prescaler, sjw, seg1, seg2 = struct.unpack("<5H", baseline["can_customval"])
        can.set_nominal_custom_speed(prescaler, sjw, seg1, seg2)
        verify("can_customval", struct.pack("<5H", 1, prescaler, sjw, seg1, seg2))
        can.set_nominal_speed(baseline["can_speed"][0])
        verify("can_speed", baseline["can_speed"])
        verify("can_customval", b"\0\0" + baseline["can_customval"][2:])

        _, prescaler, sjw, seg1, seg2 = struct.unpack("<5H", baseline["fd_customval"])
        can.set_data_custom_speed(prescaler, sjw, seg1, seg2)
        verify("fd_customval", struct.pack("<5H", 1, prescaler, sjw, seg1, seg2))
        can.set_data_speed(baseline["fd_speed"][0])
        verify("fd_speed", baseline["fd_speed"])
        verify("fd_customval", b"\0\0" + baseline["fd_customval"][2:])

        for setter, name in [
            (can.set_fd_standard, "standard"),
            (can.set_terminal_resistance, "term_res"),
            (can.set_busoff_auto_recovery, "busoff_recovery"),
            (can.set_auto_retransmission, "auto_retrans"),
        ]:
            setter(baseline[name][0])
            verify(name, baseline[name])

        version = baseline["hardware_version"].split(b"\0", 1)[0].decode("ascii")
        if version.encode().ljust(10, b"\0") != baseline["hardware_version"]:
            raise JCanError("当前硬件版本不是 DLL 支持的 NUL 填充 ASCII 格式")
        can.set_hardware_version(version)
        verify("hardware_version", baseline["hardware_version"])
        can.set_device_id(struct.unpack("<H", baseline["id"])[0])
        verify("id", baseline["id"])
    finally:
        for name, _ in CONFIG_FIELDS:
            can._config_set(name, baseline[name])

    for name, size in CONFIG_FIELDS:
        verify(name, baseline[name])
    if verbose:
        print("config-roundtrip-test: OK（配置已恢复）")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="选择指定 USB 序列号")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="枚举 JTool-CAN")
    start = sub.add_parser("start", help="启动 CAN")
    start.add_argument("--mode", choices=MODES, default="normal")
    sub.add_parser("stop", help="停止 CAN")
    send = sub.add_parser("send", help="发送 CAN/CAN FD 帧")
    send.add_argument("id", type=lambda value: parse_hex(value, "CAN ID", 0x1FFFFFFF))
    send.add_argument("data", nargs="*", type=lambda value: parse_hex(value, "数据字节", 0xFF))
    send.add_argument("-e", "--extended", action="store_true")
    send.add_argument("-f", "--fd", action="store_true")
    send.add_argument("-r", "--remote", action="store_true")
    send.add_argument("-b", "--brs", action="store_true")
    recv = sub.add_parser("recv", help="启动 CAN 并接收帧，Ctrl-C 退出")
    recv.add_argument("-n", "--count", type=int, default=0, help="收到 N 帧后退出，0 表示持续接收")
    recv.add_argument("--mode", choices=MODES, default="normal")
    recv.add_argument("--timeout", type=float, default=0, help="总超时秒数，0 表示不超时")
    sub.add_parser("loopback-test", help="在 silent-loopback 模式验证收发链路")
    benchmark = sub.add_parser("loopback-benchmark", help="测量 silent-loopback 单帧往返吞吐和延迟")
    benchmark.add_argument("--count", type=lambda value: parse_int(value, "帧数", 1, 10000), default=100)
    info = sub.add_parser("canopen-info", help="只读查询 CANopen 标准身份对象")
    info.add_argument("node", type=lambda value: parse_hex(value, "Node-ID", 0x7F))
    status = sub.add_parser("cia402-status", help="只读查询 CiA 402 驱动器状态")
    status.add_argument("node", type=lambda value: parse_hex(value, "Node-ID", 0x7F))
    sub.add_parser("config-dump", help="只读输出 JCAN 当前配置原始值")
    sub.add_parser("config-roundtrip-test", help="写入、回读并恢复全部可安全验证的 JCAN 配置")
    nominal = sub.add_parser("set-nominal-speed", help="设置经典 CAN 预置仲裁相位速率编号")
    nominal.add_argument("value", type=lambda value: parse_int(value, "速率编号", 0, 0xFF))
    data = sub.add_parser("set-data-speed", help="设置 CAN FD 预置数据相位速率编号")
    data.add_argument("value", type=lambda value: parse_int(value, "速率编号", 0, 0xFF))
    nominal_custom = sub.add_parser("set-nominal-custom", help="设置经典 CAN 自定义仲裁相位时序")
    nominal_custom.add_argument("prescaler", type=int)
    nominal_custom.add_argument("sjw", type=int)
    nominal_custom.add_argument("seg1", type=int)
    nominal_custom.add_argument("seg2", type=int)
    data_custom = sub.add_parser("set-data-custom", help="设置 CAN FD 自定义数据相位时序")
    data_custom.add_argument("prescaler", type=int)
    data_custom.add_argument("sjw", type=int)
    data_custom.add_argument("seg1", type=int)
    data_custom.add_argument("seg2", type=int)
    for command, help_text in [
        ("set-fd-standard", "设置 CAN FD 标准编号 0/1"),
        ("set-terminal-resistance", "启用/关闭终端电阻"),
        ("set-busoff-recovery", "启用/关闭 Bus-Off 自动恢复"),
        ("set-auto-retransmission", "启用/关闭自动重传"),
    ]:
        setting = sub.add_parser(command, help=help_text)
        setting.add_argument("value", choices=(0, 1), type=int)
    hardware = sub.add_parser("set-hardware-version", help="设置 JCAN 硬件版本字符串（最多 9 个 ASCII 字符）")
    hardware.add_argument("version")
    device_id = sub.add_parser("set-device-id", help="设置 JCAN 设备 ID")
    device_id.add_argument("value", type=lambda value: parse_int(value, "设备 ID", 0, 0xFFFF))
    sub.add_parser("reboot", help="重启 JCAN")
    sub.add_parser("into-boot", help="设置停留 Bootloader 并重启")
    sub.add_parser("self-test", help="运行协议编解码自检（不访问硬件）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    usb = LibUsb()
    if args.command == "scan":
        found = False
        for dev, desc, bus, address in usb.devices():
            found = True
            serial = usb.serial(dev, desc)
            print(f"{bus:03d}:{address:03d}  {VID:04x}:{PID:04x}  serial={serial or '<权限不足/未知>'}")
        return 0 if found else 1
    with JCan(usb, args.serial) as can:
        if args.command == "start":
            can.start(MODES[args.mode])
        elif args.command == "stop":
            can.stop()
        elif args.command == "send":
            can.send(args.id, bytes(args.data), fd=args.fd, remote=args.remote, brs=args.brs, extended=args.extended)
        elif args.command == "recv":
            parser = CanStreamParser()
            received = 0
            deadline = time.monotonic() + args.timeout if args.timeout else None
            can.enable_receive()
            can.start(MODES[args.mode])
            try:
                while (not args.count or received < args.count) and (deadline is None or time.monotonic() < deadline):
                    for frame in parser.feed(can.receive()):
                        print_frame(frame)
                        received += 1
                        if args.count and received >= args.count:
                            break
            except KeyboardInterrupt:
                pass
            finally:
                can.stop()
        elif args.command == "loopback-test":
            for frame in run_loopback_test(can):
                print_frame(frame)
            print("loopback-test: OK")
        elif args.command == "loopback-benchmark":
            result = loopback_benchmark(can, args.count)
            latency = result["latency_ms"]
            print(f"count={result['count']} elapsed_s={result['elapsed_s']:.6f} fps={result['fps']:.1f} "
                  f"latency_ms=min:{latency['min']:.3f} avg:{latency['avg']:.3f} "
                  f"p95:{latency['p95']:.3f} max:{latency['max']:.3f}")
        elif args.command == "canopen-info":
            parser = CanStreamParser()
            can.enable_receive()
            can.start(MODES["normal"])
            try:
                objects = [(0x1000, 0, "device_type"), (0x1001, 0, "error_register"), (0x1018, 0, "identity_entries")]
                for index, subindex, name in objects:
                    value, _ = sdo_read(can, parser, args.node, index, subindex)
                    print(f"{name}: 0x{int.from_bytes(value, 'little'):0{len(value) * 2}X}")
                entries = int.from_bytes(value, "little")
                for subindex, name in enumerate(("vendor_id", "product_code", "revision", "serial"), 1):
                    if subindex > entries:
                        break
                    value, _ = sdo_read(can, parser, args.node, 0x1018, subindex)
                    print(f"{name}: 0x{int.from_bytes(value, 'little'):0{len(value) * 2}X}")
            finally:
                can.stop()
        elif args.command == "cia402-status":
            parser = CanStreamParser()
            can.enable_receive()
            can.start(MODES["normal"])
            try:
                for index, name, signed in [(0x603F, "error_code", False), (0x6041, "statusword", False), (0x6061, "mode_display", True), (0x6064, "position_actual", True)]:
                    value, raw = sdo_read(can, parser, args.node, index, 0)
                    number = int.from_bytes(value, "little", signed=signed)
                    print(f"{name}: {number} (0x{int.from_bytes(value, 'little'):0{len(value) * 2}X})  raw={raw.hex(' ')}")
            finally:
                can.stop()
        elif args.command == "config-dump":
            for name, size in CONFIG_FIELDS:
                print(f"{name}: {can._config_get(name, size).hex(' ')}")
        elif args.command == "config-roundtrip-test":
            config_roundtrip_test(can)
        elif args.command == "set-nominal-speed":
            can.set_nominal_speed(args.value)
        elif args.command == "set-data-speed":
            can.set_data_speed(args.value)
        elif args.command == "set-nominal-custom":
            can.set_nominal_custom_speed(args.prescaler, args.sjw, args.seg1, args.seg2)
        elif args.command == "set-data-custom":
            can.set_data_custom_speed(args.prescaler, args.sjw, args.seg1, args.seg2)
        elif args.command == "set-fd-standard":
            can.set_fd_standard(args.value)
        elif args.command == "set-terminal-resistance":
            can.set_terminal_resistance(args.value)
        elif args.command == "set-busoff-recovery":
            can.set_busoff_auto_recovery(args.value)
        elif args.command == "set-auto-retransmission":
            can.set_auto_retransmission(args.value)
        elif args.command == "set-hardware-version":
            can.set_hardware_version(args.version)
        elif args.command == "set-device-id":
            can.set_device_id(args.value)
        elif args.command == "reboot":
            can.reboot()
        elif args.command == "into-boot":
            can.into_boot()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JCanError as exc:
        print(f"jcan: {exc}", file=sys.stderr)
        raise SystemExit(1)
