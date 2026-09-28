use rusb::{Context, DeviceHandle, UsbContext};
use std::fmt::{self, Write as _};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const VID: u16 = 0xffff;
pub const PID: u16 = 0x0004;
const EP_COMMAND_OUT: u8 = 0x02;
const EP_COMMAND_IN: u8 = 0x82;
const EP_STREAM_IN: u8 = 0x83;
const USB_TIMEOUT: Duration = Duration::from_millis(4500);
const DLC_LENGTHS: [usize; 16] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64];
pub const CONFIG_FIELDS: [(&str, usize); 10] = [
    ("can_speed", 1),
    ("can_customval", 10),
    ("fd_speed", 1),
    ("fd_customval", 10),
    ("standard", 1),
    ("term_res", 1),
    ("busoff_recovery", 1),
    ("auto_retrans", 1),
    ("hardware_version", 10),
    ("id", 2),
];

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone)]
pub struct Error(pub String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

impl From<rusb::Error> for Error {
    fn from(value: rusb::Error) -> Self {
        Self(format!("USB: {value}"))
    }
}

#[derive(Debug, Clone)]
pub struct DeviceInfo {
    pub serial: String,
    pub bus: u8,
    pub address: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Normal,
    Silent,
    Loopback,
    SilentLoopback,
}

impl Mode {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "normal" => Ok(Self::Normal),
            "silent" => Ok(Self::Silent),
            "loopback" => Ok(Self::Loopback),
            "silent-loopback" => Ok(Self::SilentLoopback),
            _ => Err(Error(format!("无效模式: {value}"))),
        }
    }

    fn value(self) -> u8 {
        match self {
            Self::Normal => 0,
            Self::Silent => 1,
            Self::Loopback => 2,
            Self::SilentLoopback => 3,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub id: u32,
    pub data: Vec<u8>,
    pub timestamp: u16,
    pub fd: bool,
    pub remote: bool,
    pub brs: bool,
    pub extended: bool,
}

impl Frame {
    pub fn flags(&self) -> String {
        [
            (self.extended, "EXT"),
            (self.fd, "FD"),
            (self.brs, "BRS"),
            (self.remote, "RTR"),
        ]
        .into_iter()
        .filter_map(|(enabled, name)| enabled.then_some(name))
        .collect::<Vec<_>>()
        .join("|")
    }
}

pub fn validate_frame(frame: &Frame) -> Result<()> {
    let maximum = if frame.extended { 0x1fff_ffff } else { 0x7ff };
    if frame.id > maximum {
        return Err(Error("CAN ID 超出标准帧/扩展帧范围".into()));
    }
    if frame.remote && (frame.fd || !frame.data.is_empty()) {
        return Err(Error("远程帧只支持经典 CAN 且不能携带数据".into()));
    }
    if frame.brs && !frame.fd {
        return Err(Error("BRS 只支持 CAN FD".into()));
    }
    let valid_length = if frame.fd {
        DLC_LENGTHS.contains(&frame.data.len())
    } else {
        frame.data.len() <= 8
    };
    if !valid_length {
        return Err(Error(
            "经典 CAN 长度需为 0..8；CAN FD 长度需为 0..8/12/16/20/24/32/48/64".into(),
        ));
    }
    Ok(())
}

pub fn scan() -> Result<Vec<DeviceInfo>> {
    let context = Context::new()?;
    let mut found = Vec::new();
    for device in context.devices()?.iter() {
        let descriptor = device.device_descriptor()?;
        if descriptor.vendor_id() != VID || descriptor.product_id() != PID {
            continue;
        }
        let handle = device
            .open()
            .map_err(|error| usb_identity_error(&device, error))?;
        let serial = read_serial(&device, &handle, &descriptor)?;
        found.push(DeviceInfo {
            serial,
            bus: device.bus_number(),
            address: device.address(),
        });
    }
    Ok(found)
}

fn usb_identity_error(device: &rusb::Device<Context>, error: rusb::Error) -> Error {
    let hint = if error == rusb::Error::Access {
        "；USB 权限不足，请安装仓库的 99-jcan.rules 并重新插拔设备"
    } else {
        ""
    };
    Error(format!(
        "读取 JTool-CAN USB 身份失败（{:03}:{:03}）: {error}{hint}",
        device.bus_number(),
        device.address()
    ))
}

fn read_serial(
    device: &rusb::Device<Context>,
    handle: &DeviceHandle<Context>,
    descriptor: &rusb::DeviceDescriptor,
) -> Result<String> {
    let index = descriptor.serial_number_string_index().ok_or_else(|| {
        Error(format!(
            "JTool-CAN {:03}:{:03} 没有序列号描述符",
            device.bus_number(),
            device.address()
        ))
    })?;
    let serial = handle
        .read_string_descriptor_ascii(index)
        .map_err(|error| usb_identity_error(device, error))?;
    if serial.is_empty() {
        return Err(Error(format!(
            "JTool-CAN {:03}:{:03} 序列号描述符为空",
            device.bus_number(),
            device.address()
        )));
    }
    Ok(serial)
}

pub struct JCan {
    handle: DeviceHandle<Context>,
    pub serial: String,
    pub bus: u8,
    pub address: u8,
    started: bool,
}

impl JCan {
    pub fn open(wanted_serial: &str) -> Result<Self> {
        if wanted_serial.is_empty() {
            return Err(Error("硬件操作必须指定 --serial".into()));
        }
        let context = Context::new()?;
        let mut open_error = None;
        for device in context.devices()?.iter() {
            let descriptor = device.device_descriptor()?;
            if descriptor.vendor_id() != VID || descriptor.product_id() != PID {
                continue;
            }
            let handle = match device.open() {
                Ok(handle) => handle,
                Err(error) => {
                    open_error = Some(usb_identity_error(&device, error));
                    continue;
                }
            };
            let serial = match read_serial(&device, &handle, &descriptor) {
                Ok(serial) => serial,
                Err(error) => {
                    open_error = Some(error);
                    continue;
                }
            };
            if serial != wanted_serial {
                continue;
            }
            let _ = handle.set_auto_detach_kernel_driver(true);
            handle.claim_interface(0).map_err(|error| {
                let hint = if error == rusb::Error::Access {
                    "；请安装 99-jcan.rules 后重新插拔设备"
                } else {
                    ""
                };
                Error(format!("占用 JTool-CAN USB 接口失败: {error}{hint}"))
            })?;
            return Ok(Self {
                handle,
                serial,
                bus: device.bus_number(),
                address: device.address(),
                started: false,
            });
        }
        if let Some(error) = open_error {
            return Err(error);
        }
        Err(Error(format!("未找到 JTool-CAN（序列号 {wanted_serial}）")))
    }

    fn write_bulk(&self, endpoint: u8, data: &[u8]) -> Result<()> {
        let written = self.handle.write_bulk(endpoint, data, USB_TIMEOUT)?;
        if written != data.len() {
            return Err(Error(format!("USB 短写: {written}/{}", data.len())));
        }
        Ok(())
    }

    fn read_bulk(&self, endpoint: u8, timeout: Duration, allow_timeout: bool) -> Result<Vec<u8>> {
        let mut buffer = vec![0; 4096];
        match self.handle.read_bulk(endpoint, &mut buffer, timeout) {
            Ok(length) => {
                buffer.truncate(length);
                Ok(buffer)
            }
            Err(rusb::Error::Timeout) if allow_timeout => Ok(Vec::new()),
            Err(error) => Err(error.into()),
        }
    }

    pub fn request(&self, channel: u8, command: u8, payload: &[u8]) -> Result<Vec<u8>> {
        self.write_bulk(EP_COMMAND_OUT, &packet(channel, command, payload, false))?;
        let reply = self.read_bulk(EP_COMMAND_IN, USB_TIMEOUT, false)?;
        if reply.len() < 6 {
            return Err(Error("设备回复过短".into()));
        }
        let length = u16::from_le_bytes([reply[4], reply[5]]) as usize;
        let body = &reply[6..];
        if reply[0] != 0x4a || reply[2] != 1 || reply[3] != command || body.len() != length {
            return Err(Error(format!("设备回复格式错误: {}", hex(body))));
        }
        if body.first() != Some(&1) {
            return Err(Error(format!("设备拒绝命令: {}", hex(body))));
        }
        Ok(body[1..].to_vec())
    }

    pub fn start(&mut self, mode: Mode) -> Result<()> {
        self.request(5, 1, &[mode.value()])?;
        self.started = true;
        Ok(())
    }

    pub fn stop(&mut self) -> Result<()> {
        self.request(5, 2, &[])?;
        self.started = false;
        Ok(())
    }

    pub fn leave_started(&mut self) {
        self.started = false;
    }

    pub fn enable_receive(&self) -> Result<()> {
        self.request(0, 4, &[])?;
        Ok(())
    }

    pub fn send(&self, frame: &Frame) -> Result<()> {
        validate_frame(frame)?;
        let mut payload = Vec::with_capacity(73);
        payload.extend([
            frame.fd as u8,
            frame.remote as u8,
            frame.brs as u8,
            frame.extended as u8,
        ]);
        payload.extend(frame.id.to_le_bytes());
        payload.push(frame.data.len() as u8);
        payload.extend(&frame.data);
        payload.resize(73, 0);
        self.request(5, 0, &payload)?;
        Ok(())
    }

    pub fn receive(&self, timeout: Duration) -> Result<Vec<u8>> {
        let outer = self.read_bulk(EP_STREAM_IN, timeout, true)?;
        Ok(stream_payload(&outer)?.to_vec())
    }

    pub fn config_get(&self, name: &str, size: usize) -> Result<Vec<u8>> {
        let mut key = name.as_bytes().to_vec();
        key.push(0);
        let body = self.request(0, 1, &key)?;
        let offset = 15;
        if body.len() < offset + size || u16::from_le_bytes([body[4], body[5]]) as usize != size {
            return Err(Error(format!("配置 {name} 回复格式错误: {}", hex(&body))));
        }
        Ok(body[offset..offset + size].to_vec())
    }

    pub fn config_set(&self, name: &str, value: &[u8]) -> Result<()> {
        let mut payload = name.as_bytes().to_vec();
        payload.push(0);
        payload.extend(value);
        self.request(0, 2, &payload)?;
        Ok(())
    }

    fn config_set_verified(&self, name: &str, value: &[u8]) -> Result<()> {
        self.config_set(name, value)?;
        let actual = self.config_get(name, value.len())?;
        if actual != value {
            return Err(Error(format!(
                "配置 {name} 回读不匹配: {} != {}",
                hex(&actual),
                hex(value)
            )));
        }
        Ok(())
    }

    pub fn config_dump(&self) -> Result<Vec<(String, Vec<u8>)>> {
        CONFIG_FIELDS
            .iter()
            .map(|(name, size)| Ok(((*name).to_string(), self.config_get(name, *size)?)))
            .collect()
    }

    pub fn set_nominal_speed(&self, value: u8) -> Result<()> {
        let mut custom = self.config_get("can_customval", 10)?;
        self.config_set_verified("can_speed", &[value])?;
        custom[..2].fill(0);
        self.config_set_verified("can_customval", &custom)
    }

    pub fn set_data_speed(&self, value: u8) -> Result<()> {
        let mut custom = self.config_get("fd_customval", 10)?;
        self.config_set_verified("fd_speed", &[value])?;
        custom[..2].fill(0);
        self.config_set_verified("fd_customval", &custom)
    }

    pub fn set_nominal_custom(&self, prescaler: u16, sjw: u16, seg1: u16, seg2: u16) -> Result<()> {
        if !(1..=512).contains(&prescaler)
            || !(1..=128).contains(&sjw)
            || !(2..=256).contains(&seg1)
            || !(2..=128).contains(&seg2)
            || sjw > seg2
        {
            return Err(Error("经典 CAN 自定义时序范围无效".into()));
        }
        self.config_set_verified("can_customval", &u16s(&[1, prescaler, sjw, seg1, seg2]))
    }

    pub fn set_data_custom(&self, prescaler: u16, sjw: u16, seg1: u16, seg2: u16) -> Result<()> {
        if !(1..=32).contains(&prescaler)
            || !(1..=16).contains(&sjw)
            || !(1..=32).contains(&seg1)
            || !(1..=16).contains(&seg2)
            || sjw > seg2
        {
            return Err(Error("CAN FD 数据相位时序范围无效".into()));
        }
        self.config_set_verified("fd_customval", &u16s(&[1, prescaler, sjw, seg1, seg2]))
    }

    pub fn set_u8(&self, name: &str, value: u8) -> Result<()> {
        if value > 1 {
            return Err(Error(format!("{name} 只接受 0 或 1")));
        }
        self.config_set_verified(name, &[value])
    }

    pub fn set_hardware_version(&self, value: &str) -> Result<()> {
        if !value.is_ascii() || value.len() > 9 {
            return Err(Error("硬件版本必须是不超过 9 字节的 ASCII".into()));
        }
        let mut bytes = value.as_bytes().to_vec();
        bytes.resize(10, 0);
        self.config_set_verified("hardware_version", &bytes)
    }

    pub fn set_device_id(&self, value: u16) -> Result<()> {
        self.config_set_verified("id", &value.to_le_bytes())
    }

    pub fn reboot(&self) -> Result<()> {
        self.request(0, 3, &[])?;
        Ok(())
    }
}

impl Drop for JCan {
    fn drop(&mut self) {
        if self.started {
            let _ = self.stop();
        }
        let _ = self.handle.release_interface(0);
    }
}

fn stream_payload(outer: &[u8]) -> Result<&[u8]> {
    if outer.is_empty() {
        return Ok(outer);
    }
    if outer.len() < 6 {
        return Err(Error(format!(
            "接收数据包过短（实际 {} 字节）: {}",
            outer.len(),
            hex(outer)
        )));
    }
    let declared = u16::from_le_bytes([outer[4], outer[5]]) as usize;
    let body = &outer[6..];
    if outer[..4] != [0x4a, 5, 1, 0xff] || body.len() != declared {
        return Err(Error(format!(
            "接收数据包格式错误（声明 {declared} 字节，实际 {} 字节，差值 {:+}）: {}",
            body.len(),
            body.len() as isize - declared as isize,
            hex(outer)
        )));
    }
    Ok(body)
}

#[derive(Default)]
pub struct StreamParser {
    buffer: Vec<u8>,
}

impl StreamParser {
    pub fn feed(&mut self, data: &[u8]) -> Vec<Frame> {
        self.buffer.extend(data);
        let mut frames = Vec::new();
        loop {
            let start = self
                .buffer
                .windows(2)
                .position(|window| window == [0xff, 0xaa]);
            let Some(start) = start else {
                let keep_ff = self.buffer.last() == Some(&0xff);
                self.buffer.clear();
                if keep_ff {
                    self.buffer.push(0xff);
                }
                break;
            };
            if start > 0 {
                self.buffer.drain(..start);
            }
            if self.buffer.len() < 3 {
                break;
            }
            let flags = self.buffer[2];
            let length = if flags & 0x20 != 0 {
                0
            } else {
                DLC_LENGTHS[(flags & 0x0f) as usize]
            };
            let total = 10 + length;
            if self.buffer.len() < total {
                break;
            }
            let raw: Vec<_> = self.buffer.drain(..total).collect();
            if crc8(&raw[..raw.len() - 1]) != raw[raw.len() - 1] {
                continue;
            }
            let frame = Frame {
                fd: flags & 0x10 != 0,
                remote: flags & 0x20 != 0,
                brs: flags & 0x40 != 0,
                extended: flags & 0x80 != 0,
                id: u32::from_le_bytes(raw[3..7].try_into().unwrap()),
                timestamp: u16::from_le_bytes(raw[7..9].try_into().unwrap()),
                data: raw[9..raw.len() - 1].to_vec(),
            };
            if validate_frame(&frame).is_ok() {
                frames.push(frame);
            }
        }
        frames
    }
}

pub fn packet(channel: u8, command: u8, payload: &[u8], response: bool) -> Vec<u8> {
    let mut result = vec![0x4a, channel, response as u8, command];
    result.extend((payload.len() as u16).to_le_bytes());
    result.extend(payload);
    result
}

pub fn crc8(data: &[u8]) -> u8 {
    data.iter().fold(0, |mut value, byte| {
        value ^= byte;
        for _ in 0..8 {
            value = if value & 0x80 != 0 {
                (value << 1) ^ 0x07
            } else {
                value << 1
            };
        }
        value
    })
}

pub fn parse_hex_u32(value: &str, maximum: u32, name: &str) -> Result<u32> {
    let text = value.trim_start_matches("0x").trim_start_matches("0X");
    let number =
        u32::from_str_radix(text, 16).map_err(|_| Error(format!("{name} 必须是十六进制数")))?;
    if number > maximum {
        return Err(Error(format!("{name} 范围必须是 0..{maximum:X}")));
    }
    Ok(number)
}

pub fn parse_number<T>(value: &str, name: &str) -> Result<T>
where
    T: TryFrom<u64>,
{
    let number = if let Some(hex) = value
        .strip_prefix("0x")
        .or_else(|| value.strip_prefix("0X"))
    {
        u64::from_str_radix(hex, 16)
    } else {
        value.parse()
    }
    .map_err(|_| Error(format!("{name} 必须是整数")))?;
    T::try_from(number).map_err(|_| Error(format!("{name} 超出范围")))
}

pub fn parse_candump_frame(value: &str) -> Result<Option<(u32, Vec<u8>)>> {
    let Some((id, data)) = value.split_once('#') else {
        return Ok(None);
    };
    if id.is_empty() || data.contains('#') {
        return Err(Error("CAN frame must use ID#DATA format".into()));
    }
    if !data.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(Error("ID#DATA payload contains a non-hex character".into()));
    }
    if !data.len().is_multiple_of(2) {
        return Err(Error("ID#DATA payload must contain complete bytes".into()));
    }
    let bytes = (0..data.len())
        .step_by(2)
        .map(|index| {
            u8::from_str_radix(&data[index..index + 2], 16)
                .map_err(|_| Error("ID#DATA payload contains a non-hex character".into()))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Some((parse_hex_u32(id, 0x1fff_ffff, "CAN ID")?, bytes)))
}

pub fn parse_bytes(values: &[String]) -> Result<Vec<u8>> {
    values
        .iter()
        .map(|value| parse_hex_u32(value, 0xff, "数据字节").map(|value| value as u8))
        .collect()
}

pub fn hex(data: &[u8]) -> String {
    data.iter()
        .enumerate()
        .fold(String::new(), |mut output, (index, byte)| {
            if index > 0 {
                output.push(' ');
            }
            let _ = write!(output, "{byte:02X}");
            output
        })
}

pub fn with_started<T>(
    can: &mut JCan,
    mode: Mode,
    operation: impl FnOnce(&mut JCan) -> Result<T>,
) -> Result<T> {
    can.start(mode)?;
    let result = operation(can);
    let cleanup = can.stop();
    match (result, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(cleanup)) => Err(Error(format!("CANStop 失败: {cleanup}"))),
        (Err(error), Err(cleanup)) => Err(Error(format!("{error}; CANStop 失败: {cleanup}"))),
    }
}

pub fn wait_frame(
    can: &JCan,
    parser: &mut StreamParser,
    id: u32,
    timeout: Duration,
) -> Result<Frame> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .min(Duration::from_millis(500));
        for frame in parser.feed(&can.receive(remaining.max(Duration::from_millis(1)))?) {
            if frame.id == id {
                return Ok(frame);
            }
        }
    }
    Err(Error(format!("等待 CAN ID {id:03X} 超时")))
}

pub fn loopback_test(can: &mut JCan) -> Result<Vec<Frame>> {
    can.enable_receive()?;
    with_started(can, Mode::SilentLoopback, |can| {
        let tests = [
            Frame {
                id: 0x123,
                data: vec![0x11, 0x22, 0x33, 0x44],
                timestamp: 0,
                fd: false,
                remote: false,
                brs: false,
                extended: false,
            },
            Frame {
                id: 0x18ff0011,
                data: (0..8).collect(),
                timestamp: 0,
                fd: false,
                remote: false,
                brs: false,
                extended: true,
            },
            Frame {
                id: 0x321,
                data: (0..12).collect(),
                timestamp: 0,
                fd: true,
                remote: false,
                brs: true,
                extended: false,
            },
        ];
        let mut parser = StreamParser::default();
        let mut received = Vec::new();
        for expected in tests {
            can.send(&expected)?;
            let frame = wait_frame(can, &mut parser, expected.id, Duration::from_secs(2))?;
            if frame.data != expected.data
                || frame.fd != expected.fd
                || frame.brs != expected.brs
                || frame.extended != expected.extended
            {
                return Err(Error("loopback 数据或标志不匹配".into()));
            }
            received.push(frame);
        }
        Ok(received)
    })
}

#[derive(Debug, Clone)]
pub struct Benchmark {
    pub count: u32,
    pub elapsed_s: f64,
    pub fps: f64,
    pub min_ms: f64,
    pub avg_ms: f64,
    pub p95_ms: f64,
    pub max_ms: f64,
}

pub fn loopback_benchmark(can: &mut JCan, count: u32) -> Result<Benchmark> {
    if !(1..=10_000).contains(&count) {
        return Err(Error("count 范围必须是 1..10000".into()));
    }
    can.enable_receive()?;
    with_started(can, Mode::SilentLoopback, |can| {
        let started = Instant::now();
        let mut parser = StreamParser::default();
        let mut latencies = Vec::with_capacity(count as usize);
        for sequence in 0..count {
            let mut data = sequence.to_le_bytes().to_vec();
            data.extend((!sequence).to_le_bytes());
            let frame = Frame {
                id: 0x555,
                data,
                timestamp: 0,
                fd: false,
                remote: false,
                brs: false,
                extended: false,
            };
            let sent = Instant::now();
            can.send(&frame)?;
            if wait_frame(can, &mut parser, frame.id, Duration::from_secs(2))?.data != frame.data {
                return Err(Error("基准帧数据不匹配".into()));
            }
            latencies.push(sent.elapsed().as_secs_f64() * 1000.0);
        }
        let elapsed_s = started.elapsed().as_secs_f64();
        latencies.sort_by(f64::total_cmp);
        let sum: f64 = latencies.iter().sum();
        Ok(Benchmark {
            count,
            elapsed_s,
            fps: count as f64 / elapsed_s,
            min_ms: latencies[0],
            avg_ms: sum / count as f64,
            p95_ms: latencies[((95 * count - 1) / 100) as usize],
            max_ms: latencies[latencies.len() - 1],
        })
    })
}

pub fn parse_sdo_upload(data: &[u8], index: u16, subindex: u8) -> Result<Vec<u8>> {
    if data.len() != 8 || data[1..4] != [index.to_le_bytes()[0], index.to_le_bytes()[1], subindex] {
        return Err(Error("SDO 回复格式或索引不匹配".into()));
    }
    if data[0] == 0x80 {
        return Err(Error(format!(
            "SDO abort 0x{:08X}",
            u32::from_le_bytes(data[4..8].try_into().unwrap())
        )));
    }
    if data[0] & 0xe0 != 0x40 || data[0] & 0x02 == 0 {
        return Err(Error("暂不支持分段 SDO upload".into()));
    }
    let size = if data[0] & 1 != 0 {
        4 - ((data[0] >> 2) & 3) as usize
    } else {
        4
    };
    Ok(data[4..4 + size].to_vec())
}

pub fn sdo_read(
    can: &JCan,
    parser: &mut StreamParser,
    node: u8,
    index: u16,
    subindex: u8,
) -> Result<(Vec<u8>, Frame)> {
    if node == 0 || node > 0x7f {
        return Err(Error("CANopen Node-ID 范围必须是 1..127".into()));
    }
    let index_bytes = index.to_le_bytes();
    let mut data = vec![0x40, index_bytes[0], index_bytes[1], subindex];
    data.resize(8, 0);
    can.send(&Frame {
        id: 0x600 + node as u32,
        data,
        timestamp: 0,
        fd: false,
        remote: false,
        brs: false,
        extended: false,
    })?;
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .min(Duration::from_millis(500));
        for frame in parser.feed(&can.receive(remaining.max(Duration::from_millis(1)))?) {
            if frame.id == 0x580 + node as u32
                && !frame.extended
                && !frame.fd
                && !frame.remote
                && !frame.brs
                && frame.data.len() == 8
                && frame.data[1..4] == [index_bytes[0], index_bytes[1], subindex]
            {
                return Ok((parse_sdo_upload(&frame.data, index, subindex)?, frame));
            }
        }
    }
    Err(Error(format!(
        "等待 CANopen SDO 0x{index:04X}:{subindex:02X} 超时"
    )))
}

pub fn config_apply(
    can: &JCan,
    operation: impl FnOnce(&JCan) -> Result<()>,
) -> Result<Vec<(String, Vec<u8>)>> {
    let baseline = can.config_dump()?;
    match operation(can).and_then(|_| can.config_dump()) {
        Ok(values) => Ok(values),
        Err(error) => {
            let mut restore_errors = Vec::new();
            for (name, value) in &baseline {
                if let Err(restore) = can.config_set(name, value) {
                    restore_errors.push(format!("{name}: {restore}"));
                }
            }
            if restore_errors.is_empty() {
                Err(error)
            } else {
                Err(Error(format!(
                    "{error}; 配置恢复失败: {}",
                    restore_errors.join("; ")
                )))
            }
        }
    }
}

pub fn config_roundtrip_test(can: &JCan) -> Result<Vec<(String, Vec<u8>)>> {
    let baseline = can.config_dump()?;
    let result = config_apply(can, |can| {
        for (name, value) in &baseline {
            can.config_set_verified(name, value)?;
        }
        Ok(())
    })?;
    if result != baseline {
        return Err(Error("配置往返后未恢复基线".into()));
    }
    Ok(result)
}

pub fn periodic_send(
    can: &mut JCan,
    frame: &Frame,
    period_ms: u64,
    count: u32,
    cancelled: &AtomicBool,
) -> Result<(u32, u32, f64, f64, bool)> {
    if !(10..=60_000).contains(&period_ms) || !(1..=1500).contains(&count) {
        return Err(Error("周期范围 10..60000 ms，次数范围 1..1500".into()));
    }
    validate_frame(frame)?;
    with_started(can, Mode::Normal, |can| {
        let period = Duration::from_millis(period_ms);
        let mut deadline = Instant::now();
        let mut missed = 0;
        let mut sent = 0;
        let mut jitter_sum = 0.0;
        let mut jitter_max: f64 = 0.0;
        for _ in 0..count {
            while Instant::now() < deadline && !cancelled.load(Ordering::Relaxed) {
                thread::sleep(
                    deadline
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(50)),
                );
            }
            if cancelled.load(Ordering::Relaxed) {
                break;
            }
            let actual = Instant::now();
            let jitter = actual.saturating_duration_since(deadline).as_secs_f64() * 1000.0;
            jitter_sum += jitter;
            jitter_max = jitter_max.max(jitter);
            can.send(frame)?;
            sent += 1;
            deadline += period;
            let after = Instant::now();
            if after > deadline {
                let skipped =
                    (after.duration_since(deadline).as_millis() / period.as_millis()) as u32 + 1;
                missed += skipped;
                deadline += period * skipped;
            }
        }
        Ok((
            sent,
            missed,
            if sent == 0 {
                0.0
            } else {
                jitter_sum / sent as f64
            },
            jitter_max,
            cancelled.load(Ordering::Relaxed),
        ))
    })
}

pub fn self_test() -> Result<()> {
    if packet(5, 1, &[0], false) != b"J\x05\0\x01\x01\0\0" {
        return Err(Error("命令封包自检失败".into()));
    }
    let mut raw = vec![0xff, 0xaa, 0x83];
    raw.extend(0x18ff0011u32.to_le_bytes());
    raw.extend(42u16.to_le_bytes());
    raw.extend([0x11, 0x22, 0x33]);
    raw.push(crc8(&raw));
    let mut parser = StreamParser::default();
    if !parser.feed(&raw[..4]).is_empty() {
        return Err(Error("分片解析自检失败".into()));
    }
    let frames = parser.feed(&raw[4..]);
    if frames.len() != 1 || frames[0].id != 0x18ff0011 || frames[0].data != [0x11, 0x22, 0x33] {
        return Err(Error("CAN 帧解析自检失败".into()));
    }
    if parse_sdo_upload(&[0x43, 0x18, 0x10, 1, 0x78, 0x56, 0x34, 0x12], 0x1018, 1)?
        != [0x78, 0x56, 0x34, 0x12]
    {
        return Err(Error("SDO 解析自检失败".into()));
    }
    Ok(())
}

fn u16s(values: &[u16]) -> Vec<u8> {
    values
        .iter()
        .flat_map(|value| value.to_le_bytes())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stream_payload_keeps_strict_lengths_and_raw_evidence() {
        let old = include_bytes!("../tests/data/receive_20260915.bin");
        let error = stream_payload(old).unwrap_err().to_string();
        assert!(error.contains("声明 3072 字节，实际 3648 字节，差值 +576"));
        assert!(error.ends_with(&hex(old)));
        assert!(StreamParser::default().feed(&old[6..]).is_empty());

        let valid = packet(5, 0xff, &[1, 2, 3], true);
        assert_eq!(stream_payload(&valid).unwrap(), [1, 2, 3]);
        assert!(stream_payload(&[]).unwrap().is_empty());
        for length in 1..valid.len() {
            assert!(stream_payload(&valid[..length]).is_err());
        }
        let mut extra = valid.clone();
        extra.push(0);
        assert!(stream_payload(&extra).is_err());
        for index in 0..4 {
            let mut bad_header = valid.clone();
            bad_header[index] ^= 1;
            assert!(stream_payload(&bad_header).is_err());
        }
    }

    #[test]
    fn protocol_roundtrip_and_validation() {
        assert_eq!(packet(5, 1, &[0], false), b"J\x05\0\x01\x01\0\0");
        let mut raw = vec![0xff, 0xaa, 0x83];
        raw.extend(0x18ff0011u32.to_le_bytes());
        raw.extend(42u16.to_le_bytes());
        raw.extend([0x11, 0x22, 0x33]);
        raw.push(crc8(&raw));
        let mut parser = StreamParser::default();
        assert!(parser.feed(&raw[..4]).is_empty());
        let frames = parser.feed(&raw[4..]);
        assert_eq!(frames[0].id, 0x18ff0011);
        assert_eq!(frames[0].data, [0x11, 0x22, 0x33]);
        assert_eq!(
            parse_sdo_upload(&[0x43, 0x18, 0x10, 1, 0x78, 0x56, 0x34, 0x12], 0x1018, 1).unwrap(),
            [0x78, 0x56, 0x34, 0x12]
        );
        assert!(
            validate_frame(&Frame {
                id: 0x800,
                data: vec![],
                timestamp: 0,
                fd: false,
                remote: false,
                brs: false,
                extended: false
            })
            .is_err()
        );
        assert_eq!(
            parse_candump_frame("601#2B17100000000000").unwrap(),
            Some((0x601, vec![0x2b, 0x17, 0x10, 0, 0, 0, 0, 0]))
        );
        assert!(parse_candump_frame("601#123").is_err());
        assert!(parse_candump_frame("601##01").is_err());
        assert!(parse_candump_frame("601#中文").is_err());
    }
}
