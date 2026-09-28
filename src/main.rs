use jcan_cli::{
    Error, Frame, JCan, Mode, Result, StreamParser, VERSION, config_apply, config_roundtrip_test,
    hex, loopback_benchmark, loopback_test, parse_bytes, parse_candump_frame, parse_hex_u32,
    parse_number, periodic_send, scan, sdo_read, self_test, with_started,
};
use std::env;
use std::io::IsTerminal;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

mod session;
mod tui;

const HELP: &str = r#"JCAN 1.0 native CLI

Usage:
  jcan [--json] --help | -h | help
  jcan [--json] COMMAND --help
  jcan [--json] scan
  jcan [--json] self-test
  jcan [--json] --serial SERIAL config-get
  jcan [--json] --serial SERIAL config-set FIELD VALUE...
  jcan [--json] --serial SERIAL config-test
  jcan [--json] --serial SERIAL session [--mode MODE] [--reconnect-ms N] [--receive]
  jcan [--json] --serial SERIAL send ID [BYTE...] | ID#DATA [--extended] [--fd] [--brs] [--remote] [--settle-ms N]
  jcan [--json] --serial SERIAL capture [--duration-ms N] [--max-frames N] [--mode silent|normal|loopback|silent-loopback]
  jcan [--json] --serial SERIAL periodic ID PERIOD_MS COUNT [BYTE...] [--extended] [--fd] [--brs] [--remote]
  jcan [--json] --serial SERIAL loopback-test
  jcan [--json] --serial SERIAL loopback-benchmark [--count N]
  jcan [--json] --serial SERIAL sdo-read NODE INDEX SUBINDEX
  jcan [--json] --serial SERIAL canopen-info NODE
  jcan [--json] --serial SERIAL cia402-status NODE
  jcan [--json] --serial SERIAL start [--mode MODE]
  jcan [--json] --serial SERIAL stop
  jcan [--json] --serial SERIAL reboot [--timeout-s N]

Config fields:
  nominal-speed N | data-speed N
  nominal-custom PRESCALER SJW SEG1 SEG2
  data-custom PRESCALER SJW SEG1 SEG2
  fd-standard 0|1 | terminal-resistance 0|1
  busoff-recovery 0|1 | auto-retransmission 0|1
  hardware-version ASCII | device-id N

Configuration values (config-get returns raw hexadecimal bytes):
  can_speed / fd_speed: nominal/data bitrate preset index, NOT a bitrate.
    Reference mapping below uses zero-based indices from supplied GUI screenshots.
    Provisional: not individually verified by device readback; firmware may differ.

    Hex   Decimal   can_speed (nominal-speed)   fd_speed (data-speed)
    00      0          5 kbit/s                  100 kbit/s
    01      1         10 kbit/s                  125 kbit/s
    02      2         20 kbit/s                  200 kbit/s
    03      3         40 kbit/s                  250 kbit/s
    04      4         50 kbit/s                  300 kbit/s
    05      5         80 kbit/s                  400 kbit/s
    06      6        100 kbit/s                  500 kbit/s
    07      7        125 kbit/s                  600 kbit/s
    08      8        200 kbit/s                  800 kbit/s
    09      9        250 kbit/s                    1 Mbit/s
    0A     10        300 kbit/s                    2 Mbit/s
    0B     11        400 kbit/s                    3 Mbit/s
    0C     12        500 kbit/s                    4 Mbit/s
    0D     13        600 kbit/s                    5 Mbit/s
    0E     14        800 kbit/s                    6 Mbit/s
    0F     15          1 Mbit/s                    8 Mbit/s

    fd_speed=06: 500 kbit/s; fd_speed=0A: 2 Mbit/s (reference mapping).
    To select 2 Mbit/s: config-set data-speed 10 (or data-speed 0x0A).
  can_customval / fd_customval: five little-endian u16 values:
    enable, prescaler, SJW, SEG1, SEG2. When enabled, custom timing overrides
    the corresponding preset. nominal-speed/data-speed disable custom timing.
  standard / fd-standard: 00 (0) = ISO FD; 01 (1) = Bosch non-ISO FD.
  term_res, busoff_recovery, auto_retrans: 00 = off; 01 = on.
  config-set numeric values are decimal, or hexadecimal with a 0x prefix.

Help is available before or after a command without opening USB.
With --json, help is returned as a JSON object with data.help.
All IDs, NODE, INDEX, SUBINDEX and BYTE values are hexadecimal.
Hardware commands require an explicit USB serial number. Capture and periodic work are bounded.
One-shot send keeps CAN running for 1000 ms by default before CANStop; override with --settle-ms.
"#;

static CANCELLED: AtomicBool = AtomicBool::new(false);
const DEFAULT_SEND_SETTLE_MS: u64 = 1000;

struct Args {
    values: Vec<String>,
    json: bool,
    serial: Option<String>,
}

impl Args {
    fn new() -> Result<Self> {
        let mut values: Vec<String> = env::args().skip(1).collect();
        let json = take_flag(&mut values, "--json");
        let serial = take_option(&mut values, "--serial")?;
        Ok(Self {
            values,
            json,
            serial,
        })
    }

    fn command(&mut self) -> Result<String> {
        if self.values.is_empty() {
            return Err(Error(HELP.into()));
        }
        Ok(self.values.remove(0))
    }

    fn serial(&self) -> Result<&str> {
        self.serial
            .as_deref()
            .ok_or_else(|| Error("硬件操作必须指定 --serial".into()))
    }

    fn finish(&self) -> Result<()> {
        if self.values.is_empty() {
            Ok(())
        } else {
            Err(Error(format!("无法识别的参数: {}", self.values.join(" "))))
        }
    }
}

fn take_flag(args: &mut Vec<String>, name: &str) -> bool {
    if let Some(index) = args.iter().position(|value| value == name) {
        args.remove(index);
        true
    } else {
        false
    }
}

fn take_option(args: &mut Vec<String>, name: &str) -> Result<Option<String>> {
    let Some(index) = args.iter().position(|value| value == name) else {
        return Ok(None);
    };
    args.remove(index);
    if index >= args.len() {
        return Err(Error(format!("{name} 缺少值")));
    }
    Ok(Some(args.remove(index)))
}

fn positional(args: &mut Vec<String>, name: &str) -> Result<String> {
    if args.is_empty() {
        Err(Error(format!("缺少参数: {name}")))
    } else {
        Ok(args.remove(0))
    }
}

fn json_escape(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value.is_control() => output.push_str(&format!("\\u{:04x}", value as u32)),
            value => output.push(value),
        }
    }
    output.push('"');
    output
}

fn frame_json(frame: &Frame, direction: &str) -> String {
    format!(
        "{{\"type\":\"frame\",\"direction\":\"{direction}\",\"timestamp\":{},\"id\":{},\"id_hex\":\"{:X}\",\"data_hex\":{},\"fd\":{},\"remote\":{},\"brs\":{},\"extended\":{}}}",
        frame.timestamp,
        frame.id,
        frame.id,
        json_escape(&hex(&frame.data)),
        frame.fd,
        frame.remote,
        frame.brs,
        frame.extended,
    )
}

fn print_frame(frame: &Frame) {
    let width = if frame.extended { 8 } else { 3 };
    println!(
        "{:05}  {:0width$X}  [{}] {}  {}",
        frame.timestamp,
        frame.id,
        frame.data.len(),
        hex(&frame.data),
        frame.flags(),
        width = width,
    );
}

fn frame_from_args(args: &mut Vec<String>) -> Result<Frame> {
    let mut extended = take_flag(args, "--extended") || take_flag(args, "-e");
    let fd = take_flag(args, "--fd") || take_flag(args, "-f");
    let brs = take_flag(args, "--brs") || take_flag(args, "-b");
    let remote = take_flag(args, "--remote") || take_flag(args, "-r");
    let first = positional(args, "ID or ID#DATA")?;
    let (id, data) = match parse_candump_frame(&first)? {
        Some((id, data)) => {
            if !args.is_empty() {
                return Err(Error(
                    "ID#DATA format does not accept separate BYTE values".into(),
                ));
            }
            extended |= id > 0x7ff;
            (id, data)
        }
        None => (
            parse_hex_u32(&first, 0x1fff_ffff, "CAN ID")?,
            parse_bytes(args)?,
        ),
    };
    let frame = Frame {
        id,
        data,
        timestamp: 0,
        fd,
        remote,
        brs,
        extended,
    };
    jcan_cli::validate_frame(&frame)?;
    args.clear();
    Ok(frame)
}

fn output_ok(json: bool, operation: &str, data: &str) {
    if json {
        println!(
            "{{\"ok\":true,\"operation\":{},\"data\":{data},\"warnings\":[]}}",
            json_escape(operation)
        );
    } else {
        println!("{operation}: OK");
        if !data.is_empty() && data != "null" {
            println!("{data}");
        }
    }
}

fn run(mut args: Args) -> Result<()> {
    if args
        .values
        .iter()
        .any(|value| value == "--help" || value == "-h")
        || args.values.first().is_some_and(|value| value == "help")
    {
        if args.json {
            output_ok(true, "help", &format!("{{\"help\":{}}}", json_escape(HELP)));
        } else {
            print!("{HELP}");
        }
        return Ok(());
    }
    let command = args.command()?;
    if command == "--version" || command == "-V" || command == "version" {
        println!("jcan {VERSION}");
        return Ok(());
    }
    match command.as_str() {
        "self-test" => {
            args.finish()?;
            self_test()?;
            output_ok(args.json, "self-test", "{\"passed\":true}");
        }
        "scan" => {
            args.finish()?;
            let devices = scan()?;
            if args.json {
                let data = devices
                    .iter()
                    .map(|device| format!("{{\"serial\":{},\"bus\":{},\"address\":{},\"vid\":\"ffff\",\"pid\":\"0004\"}}", json_escape(&device.serial), device.bus, device.address))
                    .collect::<Vec<_>>()
                    .join(",");
                output_ok(true, "scan", &format!("[{data}]"));
            } else if devices.is_empty() {
                return Err(Error("未发现 JTool-CAN".into()));
            } else {
                for device in devices {
                    println!(
                        "{:03}:{:03}  ffff:0004  serial={}",
                        device.bus,
                        device.address,
                        if device.serial.is_empty() {
                            "<未知>"
                        } else {
                            &device.serial
                        }
                    );
                }
            }
        }
        "config-get" => {
            args.finish()?;
            let can = JCan::open(args.serial()?)?;
            let values = can.config_dump()?;
            if args.json {
                let data = values
                    .iter()
                    .map(|(name, value)| {
                        format!("{}:{}", json_escape(name), json_escape(&hex(value)))
                    })
                    .collect::<Vec<_>>()
                    .join(",");
                output_ok(true, "config-get", &format!("{{{data}}}"));
            } else {
                for (name, value) in values {
                    println!("{name}: {}", hex(&value));
                }
            }
        }
        "config-set" => {
            let field = positional(&mut args.values, "FIELD")?;
            let expected = match field.as_str() {
                "nominal-custom" | "data-custom" => 4,
                "nominal-speed"
                | "data-speed"
                | "fd-standard"
                | "terminal-resistance"
                | "busoff-recovery"
                | "auto-retransmission"
                | "hardware-version"
                | "device-id" => 1,
                _ => return Err(Error(format!("不支持的配置字段: {field}"))),
            };
            if args.values.len() != expected {
                return Err(Error(format!(
                    "配置字段 {field} 需要 {expected} 个值，实际为 {}",
                    args.values.len()
                )));
            }
            let can = JCan::open(args.serial()?)?;
            let values = config_apply(&can, |can| match field.as_str() {
                "nominal-speed" => can.set_nominal_speed(parse_number(
                    &positional(&mut args.values, "N")?,
                    "nominal speed",
                )?),
                "data-speed" => can.set_data_speed(parse_number(
                    &positional(&mut args.values, "N")?,
                    "data speed",
                )?),
                "nominal-custom" => can.set_nominal_custom(
                    parse_number(&positional(&mut args.values, "PRESCALER")?, "prescaler")?,
                    parse_number(&positional(&mut args.values, "SJW")?, "sjw")?,
                    parse_number(&positional(&mut args.values, "SEG1")?, "seg1")?,
                    parse_number(&positional(&mut args.values, "SEG2")?, "seg2")?,
                ),
                "data-custom" => can.set_data_custom(
                    parse_number(&positional(&mut args.values, "PRESCALER")?, "prescaler")?,
                    parse_number(&positional(&mut args.values, "SJW")?, "sjw")?,
                    parse_number(&positional(&mut args.values, "SEG1")?, "seg1")?,
                    parse_number(&positional(&mut args.values, "SEG2")?, "seg2")?,
                ),
                "fd-standard" => can.set_u8(
                    "standard",
                    parse_number(&positional(&mut args.values, "0|1")?, "fd standard")?,
                ),
                "terminal-resistance" => can.set_u8(
                    "term_res",
                    parse_number(&positional(&mut args.values, "0|1")?, "terminal resistance")?,
                ),
                "busoff-recovery" => can.set_u8(
                    "busoff_recovery",
                    parse_number(&positional(&mut args.values, "0|1")?, "busoff recovery")?,
                ),
                "auto-retransmission" => can.set_u8(
                    "auto_retrans",
                    parse_number(&positional(&mut args.values, "0|1")?, "auto retransmission")?,
                ),
                "hardware-version" => {
                    can.set_hardware_version(&positional(&mut args.values, "ASCII")?)
                }
                "device-id" => can.set_device_id(parse_number(
                    &positional(&mut args.values, "N")?,
                    "device id",
                )?),
                _ => unreachable!(),
            })?;
            args.finish()?;
            if args.json {
                let data = values
                    .iter()
                    .map(|(name, value)| {
                        format!("{}:{}", json_escape(name), json_escape(&hex(value)))
                    })
                    .collect::<Vec<_>>()
                    .join(",");
                output_ok(true, "config-set", &format!("{{{data}}}"));
            } else {
                println!("config-set: OK（已回读）");
            }
        }
        "config-test" => {
            args.finish()?;
            let can = JCan::open(args.serial()?)?;
            config_roundtrip_test(&can)?;
            output_ok(args.json, "config-test", "{\"restored\":true}");
        }
        "session" => {
            let receive_enabled = take_flag(&mut args.values, "--receive");
            let mode = Mode::parse(
                &take_option(&mut args.values, "--mode")?.unwrap_or_else(|| "normal".into()),
            )?;
            let reconnect_ms: u64 = take_option(&mut args.values, "--reconnect-ms")?
                .map(|value| parse_number(&value, "reconnect-ms"))
                .transpose()?
                .unwrap_or(2000);
            if !(100..=60_000).contains(&reconnect_ms) {
                return Err(Error("reconnect-ms 范围必须是 100..60000".into()));
            }
            args.finish()?;
            let interactive =
                !args.json && std::io::stdin().is_terminal() && std::io::stdout().is_terminal();
            session::run(
                args.serial()?.to_string(),
                mode,
                Duration::from_millis(reconnect_ms),
                receive_enabled,
                interactive,
                &CANCELLED,
            )?;
        }
        "send" => {
            let settle_ms: u64 = take_option(&mut args.values, "--settle-ms")?
                .map(|value| parse_number(&value, "settle-ms"))
                .transpose()?
                .unwrap_or(DEFAULT_SEND_SETTLE_MS);
            if settle_ms > 2000 {
                return Err(Error("settle-ms 范围必须是 0..2000".into()));
            }
            let frame = frame_from_args(&mut args.values)?;
            let mut can = JCan::open(args.serial()?)?;
            with_started(&mut can, Mode::Normal, |can| {
                can.send(&frame)?;
                if settle_ms > 0 {
                    thread::sleep(Duration::from_millis(settle_ms));
                }
                Ok(())
            })?;
            let data = format!(
                "{{\"completion\":\"usb_command_accepted\",\"settle_ms\":{settle_ms},\"frame\":{}}}",
                frame_json(&frame, "TX")
            );
            if args.json {
                output_ok(true, "send", &data);
            } else {
                println!("send: USB command accepted; held CAN for {settle_ms} ms before stop");
                print!("TX  ");
                print_frame(&frame);
            }
        }
        "capture" => {
            let duration_ms: u64 = take_option(&mut args.values, "--duration-ms")?
                .map(|value| parse_number(&value, "duration-ms"))
                .transpose()?
                .unwrap_or(1000);
            let max_frames: u32 = take_option(&mut args.values, "--max-frames")?
                .map(|value| parse_number(&value, "max-frames"))
                .transpose()?
                .unwrap_or(100);
            let mode = Mode::parse(
                &take_option(&mut args.values, "--mode")?.unwrap_or_else(|| "silent".into()),
            )?;
            if !(1..=600_000).contains(&duration_ms) || !(1..=100_000).contains(&max_frames) {
                return Err(Error(
                    "duration-ms 范围 1..600000，max-frames 范围 1..100000".into(),
                ));
            }
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            can.enable_receive()?;
            let started = Instant::now();
            let frames = with_started(&mut can, mode, |can| {
                let mut parser = StreamParser::default();
                let mut frames = Vec::new();
                let deadline = Instant::now() + Duration::from_millis(duration_ms);
                while frames.len() < max_frames as usize && Instant::now() < deadline {
                    if CANCELLED.load(Ordering::Relaxed) {
                        break;
                    }
                    let timeout = deadline
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(100))
                        .max(Duration::from_millis(1));
                    frames.extend(parser.feed(&can.receive(timeout)?));
                    frames.truncate(max_frames as usize);
                }
                Ok(frames)
            })?;
            for frame in &frames {
                if args.json {
                    println!("{}", frame_json(frame, "RX"));
                } else {
                    print_frame(frame);
                }
            }
            let summary = format!(
                "{{\"received_frames\":{},\"duration_ms\":{:.3}}}",
                frames.len(),
                started.elapsed().as_secs_f64() * 1000.0
            );
            output_ok(args.json, "capture", &summary);
        }
        "periodic" => {
            let extended =
                take_flag(&mut args.values, "--extended") || take_flag(&mut args.values, "-e");
            let fd = take_flag(&mut args.values, "--fd") || take_flag(&mut args.values, "-f");
            let brs = take_flag(&mut args.values, "--brs") || take_flag(&mut args.values, "-b");
            let remote =
                take_flag(&mut args.values, "--remote") || take_flag(&mut args.values, "-r");
            let id = parse_hex_u32(&positional(&mut args.values, "ID")?, 0x1fff_ffff, "CAN ID")?;
            let period_ms = parse_number(&positional(&mut args.values, "PERIOD_MS")?, "period-ms")?;
            let count = parse_number(&positional(&mut args.values, "COUNT")?, "count")?;
            let frame = Frame {
                id,
                data: parse_bytes(&args.values)?,
                timestamp: 0,
                fd,
                remote,
                brs,
                extended,
            };
            let mut can = JCan::open(args.serial()?)?;
            let (sent, missed, avg, max, cancelled) =
                periodic_send(&mut can, &frame, period_ms, count, &CANCELLED)?;
            output_ok(
                args.json,
                "periodic",
                &format!(
                    "{{\"sent\":{sent},\"missed_periods\":{missed},\"cancelled\":{cancelled},\"jitter_ms\":{{\"avg\":{avg:.3},\"max\":{max:.3}}}}}"
                ),
            );
        }
        "loopback-test" => {
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            let frames = loopback_test(&mut can)?;
            if args.json {
                let data = frames
                    .iter()
                    .map(|frame| frame_json(frame, "RX"))
                    .collect::<Vec<_>>()
                    .join(",");
                output_ok(true, "loopback-test", &format!("[{data}]"));
            } else {
                for frame in &frames {
                    print_frame(frame);
                }
                println!("loopback-test: OK");
            }
        }
        "loopback-benchmark" => {
            let count = take_option(&mut args.values, "--count")?
                .map(|value| parse_number(&value, "count"))
                .transpose()?
                .unwrap_or(100);
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            let result = loopback_benchmark(&mut can, count)?;
            let data = format!(
                "{{\"count\":{},\"elapsed_s\":{:.6},\"fps\":{:.1},\"latency_ms\":{{\"min\":{:.3},\"avg\":{:.3},\"p95\":{:.3},\"max\":{:.3}}}}}",
                result.count,
                result.elapsed_s,
                result.fps,
                result.min_ms,
                result.avg_ms,
                result.p95_ms,
                result.max_ms
            );
            output_ok(args.json, "loopback-benchmark", &data);
        }
        "sdo-read" => {
            let node =
                parse_hex_u32(&positional(&mut args.values, "NODE")?, 0x7f, "Node-ID")? as u8;
            let index =
                parse_hex_u32(&positional(&mut args.values, "INDEX")?, 0xffff, "index")? as u16;
            let subindex =
                parse_hex_u32(&positional(&mut args.values, "SUBINDEX")?, 0xff, "subindex")? as u8;
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            can.enable_receive()?;
            let (value, frame) = with_started(&mut can, Mode::Normal, |can| {
                sdo_read(can, &mut StreamParser::default(), node, index, subindex)
            })?;
            output_ok(
                args.json,
                "sdo-read",
                &format!(
                    "{{\"node\":{node},\"index\":{index},\"subindex\":{subindex},\"value_hex\":{},\"value_unsigned\":{},\"response\":{}}}",
                    json_escape(&hex(&value)),
                    value.iter().enumerate().fold(0u64, |sum, (shift, byte)| sum
                        | ((*byte as u64) << (shift * 8))),
                    frame_json(&frame, "RX")
                ),
            );
        }
        "canopen-info" => {
            let node =
                parse_hex_u32(&positional(&mut args.values, "NODE")?, 0x7f, "Node-ID")? as u8;
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            can.enable_receive()?;
            let values = with_started(&mut can, Mode::Normal, |can| {
                let mut parser = StreamParser::default();
                let mut values = Vec::new();
                for (index, subindex, name) in [
                    (0x1000, 0, "device_type"),
                    (0x1001, 0, "error_register"),
                    (0x1018, 0, "identity_entries"),
                ] {
                    let (value, _) = sdo_read(can, &mut parser, node, index, subindex)?;
                    values.push((name, value));
                }
                let entries = unsigned_le(&values[2].1) as u8;
                for (subindex, name) in [
                    (1, "vendor_id"),
                    (2, "product_code"),
                    (3, "revision"),
                    (4, "serial"),
                ] {
                    if subindex > entries {
                        break;
                    }
                    let (value, _) = sdo_read(can, &mut parser, node, 0x1018, subindex)?;
                    values.push((name, value));
                }
                Ok(values)
            })?;
            print_values(args.json, "canopen-info", &values);
        }
        "cia402-status" => {
            let node =
                parse_hex_u32(&positional(&mut args.values, "NODE")?, 0x7f, "Node-ID")? as u8;
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            can.enable_receive()?;
            let values = with_started(&mut can, Mode::Normal, |can| {
                let mut parser = StreamParser::default();
                let mut values = Vec::new();
                for (index, name) in [
                    (0x603f, "error_code"),
                    (0x6041, "statusword"),
                    (0x6061, "mode_display"),
                    (0x6064, "position_actual"),
                ] {
                    let (value, _) = sdo_read(can, &mut parser, node, index, 0)?;
                    values.push((name, value));
                }
                Ok(values)
            })?;
            print_values(args.json, "cia402-status", &values);
        }
        "start" => {
            let mode = Mode::parse(
                &take_option(&mut args.values, "--mode")?.unwrap_or_else(|| "normal".into()),
            )?;
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            can.start(mode)?;
            can.leave_started();
            output_ok(args.json, "start", "null");
        }
        "stop" => {
            args.finish()?;
            let mut can = JCan::open(args.serial()?)?;
            can.stop()?;
            output_ok(args.json, "stop", "null");
        }
        "reboot" => {
            let timeout_s: u64 = take_option(&mut args.values, "--timeout-s")?
                .map(|value| parse_number(&value, "timeout-s"))
                .transpose()?
                .unwrap_or(10);
            args.finish()?;
            let serial = args.serial()?.to_string();
            JCan::open(&serial)?.reboot()?;
            let deadline = Instant::now() + Duration::from_secs(timeout_s);
            while Instant::now() < deadline {
                if CANCELLED.load(Ordering::Relaxed) {
                    return Err(Error("操作已取消".into()));
                }
                thread::sleep(Duration::from_millis(250));
                if scan()?.iter().any(|device| device.serial == serial) {
                    output_ok(args.json, "reboot", "{\"reenumerated\":true}");
                    return Ok(());
                }
            }
            return Err(Error(format!("设备未在 {timeout_s} 秒内重新枚举")));
        }
        _ => return Err(Error(format!("未知命令: {command}\n\n{HELP}"))),
    }
    Ok(())
}

fn unsigned_le(value: &[u8]) -> u64 {
    value.iter().enumerate().fold(0, |result, (shift, byte)| {
        result | ((*byte as u64) << (shift * 8))
    })
}

fn print_values(json: bool, operation: &str, values: &[(&str, Vec<u8>)]) {
    if json {
        let data = values
            .iter()
            .map(|(name, value)| {
                format!(
                    "{}:{{\"value_hex\":{},\"value_unsigned\":{}}}",
                    json_escape(name),
                    json_escape(&hex(value)),
                    unsigned_le(value)
                )
            })
            .collect::<Vec<_>>()
            .join(",");
        output_ok(true, operation, &format!("{{{data}}}"));
    } else {
        for (name, value) in values {
            println!("{name}: 0x{:X} ({})", unsigned_le(value), hex(value));
        }
    }
}

fn main() {
    if let Err(error) = ctrlc::set_handler(|| CANCELLED.store(true, Ordering::Relaxed)) {
        eprintln!("jcan: 安装 Ctrl-C 处理器失败: {error}");
        std::process::exit(1);
    }
    let args = match Args::new() {
        Ok(args) => args,
        Err(error) => {
            eprintln!("jcan: {error}");
            std::process::exit(2);
        }
    };
    let json = args.json;
    if let Err(error) = run(args) {
        if json {
            eprintln!(
                "{{\"ok\":false,\"error\":{}}}",
                json_escape(&error.to_string())
            );
        } else {
            eprintln!("jcan: {error}");
        }
        std::process::exit(1);
    }
}

#[cfg(test)]
mod cli_tests {
    use super::*;

    #[test]
    fn parses_compact_send_frame() {
        assert_eq!(DEFAULT_SEND_SETTLE_MS, 1000);
        let mut args = vec!["601#2B17100000000000".to_string()];
        let frame = frame_from_args(&mut args).unwrap();
        assert_eq!(frame.id, 0x601);
        assert_eq!(frame.data, [0x2b, 0x17, 0x10, 0, 0, 0, 0, 0]);
        assert!(!frame.extended);
    }
}
