use jcan_cli::{
    Error, Frame, JCan, Mode, Result, StreamParser, config_apply, hex, parse_candump_frame,
    parse_hex_u32, parse_number,
};
use serde_json::{Map, Value, json};
use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant};

enum Input {
    Line(String),
    Eof,
}

enum Output {
    Json,
    Tui(Box<crate::tui::Tui>),
}

impl Output {
    fn new(interactive: bool) -> Result<Self> {
        if interactive {
            Ok(Self::Tui(Box::new(
                crate::tui::Tui::new().map_err(|error| Error(error.to_string()))?,
            )))
        } else {
            Ok(Self::Json)
        }
    }

    fn emit(&mut self, value: Value) -> Result<()> {
        match self {
            Self::Tui(tui) => tui.push(&value),
            Self::Json => {
                let stdout = io::stdout();
                let mut output = stdout.lock();
                serde_json::to_writer(&mut output, &value)
                    .map_err(|error| Error(error.to_string()))?;
                output
                    .write_all(b"\n")
                    .map_err(|error| Error(error.to_string()))?;
                output.flush().map_err(|error| Error(error.to_string()))?;
            }
        }
        Ok(())
    }

    fn poll_command(&mut self) -> Result<Option<String>> {
        match self {
            Self::Tui(tui) => tui.poll_command().map_err(|error| Error(error.to_string())),
            Self::Json => Ok(None),
        }
    }

    fn receive_frame(&mut self, frame: &Frame, visible: bool) -> Result<()> {
        match self {
            Self::Tui(tui) => {
                tui.push_received(frame, visible);
                Ok(())
            }
            Self::Json if visible => self.emit(frame_event(frame)),
            Self::Json => Ok(()),
        }
    }

    fn set_config_dump(&mut self, values: &[(String, Vec<u8>)]) {
        if let Self::Tui(tui) = self {
            tui.set_config_dump(values);
        }
    }

    fn log_error(&mut self, message: impl Into<String>) {
        if let Self::Tui(tui) = self {
            tui.log_error(message);
        }
    }

    fn draw(
        &mut self,
        serial: &str,
        connected: bool,
        running: bool,
        receive_enabled: bool,
        mode: Mode,
    ) -> Result<()> {
        if let Self::Tui(tui) = self {
            tui.draw(crate::tui::Status {
                serial,
                connected,
                running,
                receive_enabled,
                mode: mode_name(mode),
            })
            .map_err(|error| Error(error.to_string()))?;
        }
        Ok(())
    }
}

pub fn run(
    serial: String,
    initial_mode: Mode,
    reconnect_delay: Duration,
    initial_receive_enabled: bool,
    interactive: bool,
    cancelled: &AtomicBool,
) -> Result<()> {
    let input = (!interactive).then(input_thread);
    let mut output = Output::new(interactive)?;
    let mut can = None;
    let mut desired = true;
    let mut running = false;
    let mut receive_enabled = initial_receive_enabled;
    let mut mode = initial_mode;
    let mut parser = StreamParser::default();
    let mut next_reconnect = Instant::now();
    output.emit(json!({
        "event": "session_started",
        "serial": serial,
        "reconnect_ms": reconnect_delay.as_millis(),
        "receive_enabled": receive_enabled,
    }))?;

    loop {
        if cancelled.load(Ordering::Relaxed) {
            output.emit(json!({"event": "session_stopping", "reason": "signal"}))?;
            break;
        }

        let mut messages = input
            .as_ref()
            .map(|input| input.try_iter().collect::<Vec<_>>())
            .unwrap_or_default();
        if let Some(line) = output.poll_command()? {
            messages.push(Input::Line(line));
        }
        for message in messages {
            match message {
                Input::Eof => {
                    output.emit(json!({"event": "session_stopping", "reason": "stdin_eof"}))?;
                    return cleanup(can, running);
                }
                Input::Line(line) => {
                    if line.len() > 65_536 {
                        output.emit(
                            json!({"ok": false, "error": "input line exceeds 65536 bytes"}),
                        )?;
                        continue;
                    }
                    let request = match parse_request(&line, interactive) {
                        Ok(request) => request,
                        Err(error) => {
                            output.emit(json!({"ok": false, "error": error.to_string()}))?;
                            continue;
                        }
                    };
                    let id = request.get("id").cloned().unwrap_or(Value::Null);
                    let op = request.get("op").and_then(Value::as_str).unwrap_or("");
                    let result = handle(
                        op,
                        &request,
                        &serial,
                        &mut can,
                        &mut desired,
                        &mut running,
                        &mut receive_enabled,
                        &mut mode,
                        &mut next_reconnect,
                    );
                    match result {
                        Ok(Control::Continue(data)) => {
                            output.emit(json!({"id": id, "ok": true, "op": op, "data": data}))?;
                        }
                        Ok(Control::Shutdown) => {
                            output.emit(
                                json!({"id": id, "ok": true, "op": op, "data": {"stopped": true}}),
                            )?;
                            return cleanup(can, running);
                        }
                        Err(error) => {
                            output.emit(
                                json!({"id": id, "ok": false, "op": op, "error": error.to_string()}),
                            )?;
                        }
                    }
                }
            }
        }

        if desired && can.is_none() && Instant::now() >= next_reconnect {
            match connect(&serial, mode) {
                Ok(opened) => {
                    can = Some(opened);
                    running = true;
                    parser = StreamParser::default();
                    output.emit(
                        json!({"event": "connected", "serial": serial, "mode": mode_name(mode)}),
                    )?;
                    if matches!(output, Output::Tui(_)) {
                        match can.as_ref().expect("connected device").config_dump() {
                            Ok(values) => output.set_config_dump(&values),
                            Err(error) => {
                                output.log_error(format!("Configuration read failed: {error}"))
                            }
                        }
                    }
                }
                Err(error) => {
                    next_reconnect = Instant::now() + reconnect_delay;
                    output.emit(json!({
                        "event": "reconnecting",
                        "serial": serial,
                        "retry_ms": reconnect_delay.as_millis(),
                        "error": error.to_string(),
                    }))?;
                }
            }
        }

        if let Some(opened) = can.as_ref().filter(|_| running) {
            match opened.receive(Duration::from_millis(50)) {
                Ok(bytes) => {
                    for frame in parser.feed(&bytes) {
                        output.receive_frame(&frame, receive_enabled)?;
                    }
                }
                Err(error) => {
                    can = None;
                    running = false;
                    next_reconnect = Instant::now() + reconnect_delay;
                    output.emit(json!({
                        "event": "disconnected",
                        "serial": serial,
                        "retry_ms": reconnect_delay.as_millis(),
                        "error": error.to_string(),
                    }))?;
                }
            }
        } else {
            thread::sleep(Duration::from_millis(20));
        }
        output.draw(&serial, can.is_some(), running, receive_enabled, mode)?;
    }

    cleanup(can, running)
}

enum Control {
    Continue(Value),
    Shutdown,
}

const INTERACTIVE_HELP: &str = r#"Commands:
  /status                 connection and CAN state
  /config                 read all configuration
  /config set FIELD VALUE...
  /send ID [BYTE...] | ID#DATA [--extended] [--fd] [--brs] [--remote]
  /rx on|off              show or hide received frames
  /timestamp on|off       show or hide elapsed traffic timestamps
  /status-format human|raw
  /mode MODE              normal|silent|loopback|silent-loopback
  /start [MODE]  /stop
  /connect  /disconnect
  /quit

Config fields: nominal-speed, data-speed, nominal-custom, data-custom,
  fd-standard, terminal-resistance, busoff-recovery, auto-retransmission,
  hardware-version, device-id"#;

#[allow(clippy::too_many_arguments)]
fn handle(
    op: &str,
    request: &Map<String, Value>,
    serial: &str,
    can: &mut Option<JCan>,
    desired: &mut bool,
    running: &mut bool,
    receive_enabled: &mut bool,
    mode: &mut Mode,
    next_reconnect: &mut Instant,
) -> Result<Control> {
    match op {
        "help" => Ok(Control::Continue(json!({"text": INTERACTIVE_HELP}))),
        "ping" => Ok(Control::Continue(json!({"pong": true}))),
        "status" => Ok(Control::Continue(json!({
            "serial": serial,
            "connected": can.is_some(),
            "desired": *desired,
            "running": *running,
            "receive_enabled": *receive_enabled,
            "mode": mode_name(*mode),
        }))),
        "connect" => {
            *desired = true;
            *next_reconnect = Instant::now();
            Ok(Control::Continue(json!({"desired": true})))
        }
        "disconnect" => {
            *desired = false;
            *running = false;
            *can = None;
            Ok(Control::Continue(json!({"connected": false})))
        }
        "start" => {
            let selected = request
                .get("mode")
                .and_then(Value::as_str)
                .map(Mode::parse)
                .transpose()?
                .unwrap_or(*mode);
            let opened = connected(can)?;
            if *running {
                opened.stop()?;
                *running = false;
            }
            opened.start(selected)?;
            *mode = selected;
            *running = true;
            Ok(Control::Continue(
                json!({"running": true, "mode": mode_name(*mode)}),
            ))
        }
        "stop" => {
            connected(can)?.stop()?;
            *running = false;
            Ok(Control::Continue(json!({"running": false})))
        }
        "set_mode" => {
            let selected = Mode::parse(required_str(request, "mode")?)?;
            let opened = connected(can)?;
            if *running {
                opened.stop()?;
                *running = false;
                opened.start(selected)?;
                *running = true;
            }
            *mode = selected;
            Ok(Control::Continue(json!({"mode": mode_name(*mode)})))
        }
        "receive" => {
            *receive_enabled = required_bool(request, "enabled")?;
            Ok(Control::Continue(
                json!({"receive_enabled": *receive_enabled}),
            ))
        }
        "config_get" => {
            let mut values = Map::new();
            for (name, value) in connected(can)?.config_dump()? {
                values.insert(name, Value::String(hex(&value)));
            }
            Ok(Control::Continue(Value::Object(values)))
        }
        "config_set" => config_set(request, can, running, *mode, next_reconnect),
        "send" => {
            if !*running {
                return Err(Error("CAN is stopped".into()));
            }
            let frame = request_frame(request)?;
            connected(can)?.send(&frame)?;
            Ok(Control::Continue(json!({
                "completion": "usb_command_accepted",
                "frame": frame_value(&frame),
            })))
        }
        "shutdown" => Ok(Control::Shutdown),
        "" => Err(Error("missing op".into())),
        _ => Err(Error(format!("unsupported session op: {op}"))),
    }
}

fn connect(serial: &str, mode: Mode) -> Result<JCan> {
    let mut can = JCan::open(serial)?;
    can.enable_receive()?;
    can.start(mode)?;
    Ok(can)
}

fn connected(can: &mut Option<JCan>) -> Result<&mut JCan> {
    can.as_mut()
        .ok_or_else(|| Error("device is not connected".into()))
}

enum ConfigChange {
    NominalSpeed(u8),
    DataSpeed(u8),
    NominalCustom(u16, u16, u16, u16),
    DataCustom(u16, u16, u16, u16),
    U8(&'static str, u8),
    HardwareVersion(String),
    DeviceId(u16),
}

impl ConfigChange {
    fn parse(request: &Map<String, Value>) -> Result<Self> {
        let field = required_str(request, "field")?;
        let values: Vec<&Value> = match request.get("values") {
            Some(Value::Array(values)) => values.iter().collect(),
            Some(_) => return Err(Error("values must be an array".into())),
            None => request.get("value").into_iter().collect(),
        };
        let expected = if matches!(
            field,
            "nominal-custom" | "can_customval" | "data-custom" | "fd_customval"
        ) {
            4
        } else {
            1
        };
        if values.len() != expected {
            return Err(Error(format!(
                "config field {field} requires {expected} value(s)"
            )));
        }
        let text = |index: usize| value_text(values[index]);
        Ok(match field {
            "nominal-speed" | "can_speed" => Self::NominalSpeed(parse_number(&text(0)?, field)?),
            "data-speed" | "fd_speed" => Self::DataSpeed(parse_number(&text(0)?, field)?),
            "nominal-custom" | "can_customval" => Self::NominalCustom(
                parse_number(&text(0)?, "prescaler")?,
                parse_number(&text(1)?, "sjw")?,
                parse_number(&text(2)?, "seg1")?,
                parse_number(&text(3)?, "seg2")?,
            ),
            "data-custom" | "fd_customval" => Self::DataCustom(
                parse_number(&text(0)?, "prescaler")?,
                parse_number(&text(1)?, "sjw")?,
                parse_number(&text(2)?, "seg1")?,
                parse_number(&text(3)?, "seg2")?,
            ),
            "fd-standard" | "standard" => Self::U8("standard", parse_number(&text(0)?, field)?),
            "terminal-resistance" | "term_res" => {
                Self::U8("term_res", parse_number(&text(0)?, field)?)
            }
            "busoff-recovery" | "busoff_recovery" => {
                Self::U8("busoff_recovery", parse_number(&text(0)?, field)?)
            }
            "auto-retransmission" | "auto_retrans" => {
                Self::U8("auto_retrans", parse_number(&text(0)?, field)?)
            }
            "hardware-version" | "hardware_version" => Self::HardwareVersion(text(0)?),
            "device-id" | "id" => Self::DeviceId(parse_number(&text(0)?, field)?),
            _ => return Err(Error(format!("unsupported config field: {field}"))),
        })
    }

    fn apply(&self, can: &JCan) -> Result<()> {
        match self {
            Self::NominalSpeed(value) => can.set_nominal_speed(*value),
            Self::DataSpeed(value) => can.set_data_speed(*value),
            Self::NominalCustom(a, b, c, d) => can.set_nominal_custom(*a, *b, *c, *d),
            Self::DataCustom(a, b, c, d) => can.set_data_custom(*a, *b, *c, *d),
            Self::U8(name, value) => can.set_u8(name, *value),
            Self::HardwareVersion(value) => can.set_hardware_version(value),
            Self::DeviceId(value) => can.set_device_id(*value),
        }
    }
}

fn value_text(value: &Value) -> Result<String> {
    match value {
        Value::String(value) => Ok(value.clone()),
        Value::Number(value) => Ok(value.to_string()),
        _ => Err(Error("config values must be strings or numbers".into())),
    }
}

fn config_set(
    request: &Map<String, Value>,
    can: &mut Option<JCan>,
    running: &mut bool,
    mode: Mode,
    next_reconnect: &mut Instant,
) -> Result<Control> {
    let change = ConfigChange::parse(request)?;
    let restart = *running;
    if restart {
        if let Err(error) = connected(can)?.stop() {
            *can = None;
            *running = false;
            *next_reconnect = Instant::now();
            return Err(error);
        }
        *running = false;
    }
    let result = config_apply(connected(can)?, |opened| change.apply(opened));
    if restart {
        if let Err(error) = connected(can)?.start(mode) {
            *can = None;
            *next_reconnect = Instant::now();
            return Err(match result {
                Ok(_) => error,
                Err(config_error) => Error(format!("{config_error}; CAN restart failed: {error}")),
            });
        }
        *running = true;
    }
    let mut readback = Map::new();
    for (name, value) in result? {
        readback.insert(name, Value::String(hex(&value)));
    }
    Ok(Control::Continue(json!({
        "can_restarted": restart,
        "readback": readback,
    })))
}

fn parse_request(line: &str, interactive: bool) -> Result<Map<String, Value>> {
    if line.trim_start().starts_with('/') {
        return slash_request(line);
    }
    match serde_json::from_str::<Value>(line) {
        Ok(Value::Object(request)) => Ok(request),
        Ok(_) => Err(Error("request must be a JSON object".into())),
        Err(error) if interactive => Err(Error(format!("unknown command; use /help ({error})"))),
        Err(error) => Err(Error(format!("invalid JSON: {error}"))),
    }
}

fn slash_request(line: &str) -> Result<Map<String, Value>> {
    let mut words = line.split_whitespace();
    let command = words.next().unwrap_or("");
    let args: Vec<&str> = words.collect();
    let request = match command {
        "/help" => json!({"op":"help"}),
        "/ping" => json!({"op":"ping"}),
        "/status" => json!({"op":"status"}),
        "/config" if args.is_empty() || args == ["get"] => json!({"op":"config_get"}),
        "/config" if args.first() == Some(&"set") && args.len() >= 3 => {
            json!({"op":"config_set", "field":args[1], "values":args[2..]})
        }
        "/rx" if args.len() == 1 => json!({"op":"receive", "enabled":on_off(args[0])?}),
        "/send" if !args.is_empty() => {
            let flags: Vec<&str> = args
                .iter()
                .copied()
                .filter(|value| value.starts_with("--"))
                .collect();
            let separate_data: Vec<&str> = args[1..]
                .iter()
                .copied()
                .filter(|value| !value.starts_with("--"))
                .collect();
            let (can_id, data_hex, inferred_extended) = match parse_candump_frame(args[0])? {
                Some((id, data)) => {
                    if !separate_data.is_empty() {
                        return Err(Error(
                            "ID#DATA format does not accept separate BYTE values".into(),
                        ));
                    }
                    (format!("{id:X}"), hex(&data), id > 0x7ff)
                }
                None => (args[0].to_string(), separate_data.join(" "), false),
            };
            json!({
                "op":"send", "can_id":can_id, "data_hex":data_hex,
                "extended":flags.contains(&"--extended") || inferred_extended, "fd":flags.contains(&"--fd"),
                "brs":flags.contains(&"--brs"), "remote":flags.contains(&"--remote")
            })
        }
        "/mode" if args.len() == 1 => json!({"op":"set_mode", "mode":args[0]}),
        "/start" if args.len() <= 1 => match args.first() {
            Some(mode) => json!({"op":"start", "mode":mode}),
            None => json!({"op":"start"}),
        },
        "/stop" => json!({"op":"stop"}),
        "/connect" => json!({"op":"connect"}),
        "/disconnect" => json!({"op":"disconnect"}),
        "/quit" | "/exit" | "/shutdown" => json!({"op":"shutdown"}),
        _ => return Err(Error("invalid command or arguments; use /help".into())),
    };
    Ok(request
        .as_object()
        .expect("slash commands create objects")
        .clone())
}

fn on_off(value: &str) -> Result<bool> {
    match value {
        "on" | "true" | "1" => Ok(true),
        "off" | "false" | "0" => Ok(false),
        _ => Err(Error("expected on or off".into())),
    }
}

fn request_frame(request: &Map<String, Value>) -> Result<Frame> {
    let id = match request.get("can_id") {
        Some(Value::Number(value)) => value
            .as_u64()
            .and_then(|value| u32::try_from(value).ok())
            .ok_or_else(|| Error("can_id must be a non-negative u32".into()))?,
        Some(Value::String(value)) => parse_hex_u32(value, 0x1fff_ffff, "can_id")?,
        _ => return Err(Error("missing can_id".into())),
    };
    let data = match request.get("data_hex") {
        None => Vec::new(),
        Some(Value::String(value)) => parse_data_hex(value)?,
        _ => return Err(Error("data_hex must be a string".into())),
    };
    let frame = Frame {
        id,
        data,
        timestamp: 0,
        fd: optional_bool(request, "fd")?,
        remote: optional_bool(request, "remote")?,
        brs: optional_bool(request, "brs")?,
        extended: optional_bool(request, "extended")?,
    };
    jcan_cli::validate_frame(&frame)?;
    Ok(frame)
}

fn parse_data_hex(value: &str) -> Result<Vec<u8>> {
    let compact: String = value
        .chars()
        .filter(|character| !character.is_ascii_whitespace() && !",;:_-".contains(*character))
        .collect();
    if !compact.len().is_multiple_of(2) {
        return Err(Error("data_hex must contain complete bytes".into()));
    }
    (0..compact.len())
        .step_by(2)
        .map(|index| {
            u8::from_str_radix(&compact[index..index + 2], 16)
                .map_err(|_| Error("data_hex contains a non-hex character".into()))
        })
        .collect()
}

fn required_str<'a>(request: &'a Map<String, Value>, name: &str) -> Result<&'a str> {
    request
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| Error(format!("{name} must be a string")))
}

fn required_bool(request: &Map<String, Value>, name: &str) -> Result<bool> {
    request
        .get(name)
        .and_then(Value::as_bool)
        .ok_or_else(|| Error(format!("{name} must be a boolean")))
}

fn optional_bool(request: &Map<String, Value>, name: &str) -> Result<bool> {
    match request.get(name) {
        None => Ok(false),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(Error(format!("{name} must be a boolean"))),
    }
}

fn input_thread() -> Receiver<Input> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) => {
                    if sender.send(Input::Line(line)).is_err() {
                        return;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = sender.send(Input::Eof);
    });
    receiver
}

fn mode_name(mode: Mode) -> &'static str {
    match mode {
        Mode::Normal => "normal",
        Mode::Silent => "silent",
        Mode::Loopback => "loopback",
        Mode::SilentLoopback => "silent-loopback",
    }
}

fn frame_event(frame: &Frame) -> Value {
    let mut value = frame_value(frame);
    value["event"] = Value::String("frame".into());
    value
}

fn frame_value(frame: &Frame) -> Value {
    json!({
        "timestamp": frame.timestamp,
        "can_id": frame.id,
        "id_hex": format!("{:X}", frame.id),
        "data_hex": hex(&frame.data),
        "fd": frame.fd,
        "remote": frame.remote,
        "brs": frame.brs,
        "extended": frame.extended,
    })
}

fn cleanup(mut can: Option<JCan>, running: bool) -> Result<()> {
    if let Some(opened) = can.as_mut().filter(|_| running) {
        opened.stop()?;
    }
    drop(can);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_session_frames_and_rejects_bad_input() {
        let request = serde_json::from_str::<Value>(
            r#"{"can_id":"18FF0011","data_hex":"01 02,FF","extended":true,"fd":false}"#,
        )
        .unwrap();
        let frame = request_frame(request.as_object().unwrap()).unwrap();
        assert_eq!(frame.id, 0x18ff0011);
        assert_eq!(frame.data, [1, 2, 0xff]);
        assert!(parse_data_hex("0").is_err());
        assert!(
            request_frame(
                serde_json::from_str::<Value>(r#"{"can_id":2048}"#)
                    .unwrap()
                    .as_object()
                    .unwrap()
            )
            .is_err()
        );
    }

    #[test]
    fn parses_interactive_commands_and_config_changes() {
        let send = parse_request("/send 123 11 22 --extended", true).unwrap();
        assert_eq!(send["op"], "send");
        assert_eq!(send["can_id"], "123");
        assert_eq!(send["data_hex"], "11 22");
        assert_eq!(send["extended"], true);
        let compact = parse_request("/send 601#2B17100000000000", true).unwrap();
        assert_eq!(compact["can_id"], "601");
        assert_eq!(compact["data_hex"], "2B 17 10 00 00 00 00 00");
        assert_eq!(compact["extended"], false);

        let set = parse_request("/config set terminal-resistance 1", true).unwrap();
        assert!(matches!(
            ConfigChange::parse(&set),
            Ok(ConfigChange::U8("term_res", 1))
        ));
        let custom = parse_request("/config set nominal-custom 1 4 89 30", true).unwrap();
        assert!(matches!(
            ConfigChange::parse(&custom),
            Ok(ConfigChange::NominalCustom(1, 4, 89, 30))
        ));
        assert!(parse_request("/rx maybe", true).is_err());
        assert!(parse_request("send 123", true).is_err());
    }
}
