use chrono::Local;
use jcan_cli::Frame as CanFrame;
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::{DefaultTerminal, Frame};
use serde_json::{Map, Value};
use std::collections::VecDeque;
use std::io;
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

const COMMANDS: &[(&str, &str)] = &[
    ("/status", "show session state"),
    ("/config", "read configuration"),
    ("/config set", "change a configuration field"),
    ("/send", "send one CAN frame"),
    ("/rx", "enable or disable RX display"),
    ("/timestamp", "show or hide traffic timestamps"),
    ("/status-format", "use human or raw config values"),
    ("/mode", "change CAN mode"),
    ("/start", "start CAN"),
    ("/stop", "stop CAN"),
    ("/connect", "enable USB reconnect"),
    ("/disconnect", "close the USB device"),
    ("/help", "show command help"),
    ("/quit", "close the session"),
];

pub struct Status<'a> {
    pub serial: &'a str,
    pub connected: bool,
    pub running: bool,
    pub receive_enabled: bool,
    pub mode: &'a str,
}

struct Traffic {
    timestamp: String,
    value: String,
}

#[derive(Clone, Copy)]
enum LogLevel {
    Debug,
    Info,
    Warn,
    Error,
}

pub struct Tui {
    terminal: DefaultTerminal,
    rx_frames: VecDeque<Traffic>,
    tx_frames: VecDeque<Traffic>,
    rx_recent: VecDeque<Instant>,
    tx_recent: VecDeque<Instant>,
    rx_total: u64,
    tx_total: u64,
    logs: VecDeque<(LogLevel, String)>,
    input: String,
    cursor: usize,
    history: Vec<String>,
    history_index: Option<usize>,
    config: Map<String, Value>,
    show_timestamps: bool,
    raw_status: bool,
    dirty: bool,
    last_draw: Instant,
}

impl Tui {
    pub fn new() -> io::Result<Self> {
        Ok(Self {
            terminal: ratatui::try_init()?,
            rx_frames: VecDeque::new(),
            tx_frames: VecDeque::new(),
            rx_recent: VecDeque::new(),
            tx_recent: VecDeque::new(),
            rx_total: 0,
            tx_total: 0,
            logs: VecDeque::from([(LogLevel::Info, "JCAN session started. Type /help.".into())]),
            input: String::new(),
            cursor: 0,
            history: Vec::new(),
            history_index: None,
            config: Map::new(),
            show_timestamps: true,
            raw_status: false,
            dirty: true,
            last_draw: Instant::now() - Duration::from_secs(1),
        })
    }

    pub fn poll_command(&mut self) -> io::Result<Option<String>> {
        while event::poll(Duration::ZERO)? {
            let Event::Key(key) = event::read()? else {
                continue;
            };
            if key.kind != KeyEventKind::Press {
                continue;
            }
            self.dirty = true;
            if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
                return Ok(Some("/quit".into()));
            }
            match key.code {
                KeyCode::Enter if !self.input.trim().is_empty() => {
                    let command = std::mem::take(&mut self.input);
                    self.cursor = 0;
                    self.history.push(command.clone());
                    self.history_index = None;
                    if self.handle_local_command(&command) {
                        continue;
                    }
                    return Ok(Some(command));
                }
                KeyCode::Char(character) => {
                    self.input.insert(self.cursor, character);
                    self.cursor += character.len_utf8();
                    self.history_index = None;
                }
                KeyCode::Backspace => {
                    if let Some(previous) = previous_boundary(&self.input, self.cursor) {
                        self.input.drain(previous..self.cursor);
                        self.cursor = previous;
                    }
                }
                KeyCode::Delete if self.cursor < self.input.len() => {
                    let next = next_boundary(&self.input, self.cursor);
                    self.input.drain(self.cursor..next);
                }
                KeyCode::Left => {
                    self.cursor = previous_boundary(&self.input, self.cursor).unwrap_or(0);
                }
                KeyCode::Right if self.cursor < self.input.len() => {
                    self.cursor = next_boundary(&self.input, self.cursor);
                }
                KeyCode::Home => self.cursor = 0,
                KeyCode::End => self.cursor = self.input.len(),
                KeyCode::Tab => self.complete_command(),
                KeyCode::Esc => {
                    self.input.clear();
                    self.cursor = 0;
                    self.history_index = None;
                }
                KeyCode::Up => self.previous_command(),
                KeyCode::Down => self.next_command(),
                _ => {}
            }
        }
        Ok(None)
    }

    pub fn push(&mut self, value: &Value) {
        self.dirty = true;
        if value["event"] == "frame" {
            self.push_frame(false, &value["id_hex"], &value["data_hex"]);
            return;
        }
        if value["ok"] == true && value["op"] == "send" {
            let frame = &value["data"]["frame"];
            self.push_frame(true, &frame["id_hex"], &frame["data_hex"]);
        }
        if value["ok"] == true && value["op"] == "config_get" {
            self.set_config(&value["data"]);
        } else if value["ok"] == true && value["op"] == "config_set" {
            self.set_config(&value["data"]["readback"]);
        }
        let (level, text) = log_text(value);
        for line in text.lines().filter(|line| !line.is_empty()) {
            self.logs.push_back((level, line.to_string()));
        }
        trim(&mut self.logs, 500);
    }

    pub fn push_received(&mut self, frame: &CanFrame, visible: bool) {
        self.rx_total += 1;
        self.rx_recent.push_back(Instant::now());
        self.dirty = true;
        if visible {
            self.rx_frames.push_back(Traffic {
                timestamp: system_timestamp(),
                value: format!("{:X}  {}", frame.id, jcan_cli::hex(&frame.data)),
            });
            trim(&mut self.rx_frames, 2_000);
        }
    }

    pub fn set_config_dump(&mut self, values: &[(String, Vec<u8>)]) {
        self.config = values
            .iter()
            .map(|(name, value)| (name.clone(), Value::String(jcan_cli::hex(value))))
            .collect();
        self.dirty = true;
    }

    pub fn log_error(&mut self, message: impl Into<String>) {
        self.logs.push_back((LogLevel::Error, message.into()));
        trim(&mut self.logs, 500);
        self.dirty = true;
    }

    pub fn draw(&mut self, status: Status<'_>) -> io::Result<()> {
        let now = Instant::now();
        let minimum_interval = if self.dirty {
            Duration::from_millis(33)
        } else {
            Duration::from_millis(250)
        };
        if now.duration_since(self.last_draw) < minimum_interval {
            return Ok(());
        }
        trim_rate(&mut self.rx_recent, now);
        trim_rate(&mut self.tx_recent, now);
        let config = config_line(&self.config, self.raw_status);
        let suggestions = suggestions(&self.input);
        let rx_rate = self.rx_recent.len();
        let tx_rate = self.tx_recent.len();
        let rx_total = self.rx_total;
        let tx_total = self.tx_total;
        let show_timestamps = self.show_timestamps;
        let rx_frames = &self.rx_frames;
        let tx_frames = &self.tx_frames;
        let logs = &self.logs;
        let input = &self.input;
        let cursor = self.cursor;
        self.terminal.draw(|frame| {
            render(
                frame,
                rx_frames,
                tx_frames,
                logs,
                input,
                cursor,
                &suggestions,
                &config,
                show_timestamps,
                rx_total,
                tx_total,
                rx_rate,
                tx_rate,
                &status,
            )
        })?;
        self.dirty = false;
        self.last_draw = now;
        Ok(())
    }

    fn push_frame(&mut self, sent: bool, id: &Value, data: &Value) {
        let traffic = Traffic {
            timestamp: system_timestamp(),
            value: format!(
                "{}  {}",
                id.as_str().unwrap_or("?"),
                data.as_str().unwrap_or("")
            ),
        };
        let now = Instant::now();
        if sent {
            self.tx_total += 1;
            self.tx_recent.push_back(now);
            self.tx_frames.push_back(traffic);
            trim(&mut self.tx_frames, 2_000);
        } else {
            self.rx_frames.push_back(traffic);
            trim(&mut self.rx_frames, 2_000);
        }
        self.dirty = true;
    }

    fn set_config(&mut self, value: &Value) {
        if let Some(values) = value.as_object() {
            self.config.clone_from(values);
            self.dirty = true;
        }
    }

    fn handle_local_command(&mut self, command: &str) -> bool {
        let words = command.split_whitespace().collect::<Vec<_>>();
        match words.as_slice() {
            ["/timestamp", value] => match on_off(value) {
                Some(enabled) => {
                    self.show_timestamps = enabled;
                    self.logs.push_back((
                        LogLevel::Info,
                        format!(
                            "Traffic timestamps {}",
                            if enabled { "enabled" } else { "disabled" }
                        ),
                    ));
                }
                None => self
                    .logs
                    .push_back((LogLevel::Error, "Usage: /timestamp on|off".into())),
            },
            ["/status-format", "human"] => {
                self.raw_status = false;
                self.logs
                    .push_back((LogLevel::Info, "StatusLine format: human".into()));
            }
            ["/status-format", "raw"] => {
                self.raw_status = true;
                self.logs
                    .push_back((LogLevel::Info, "StatusLine format: raw".into()));
            }
            ["/status-format", ..] => self
                .logs
                .push_back((LogLevel::Error, "Usage: /status-format human|raw".into())),
            _ => return false,
        }
        true
    }

    fn complete_command(&mut self) {
        let matches = command_matches(&self.input);
        if matches.len() == 1 {
            self.input = format!("{} ", matches[0].0);
            self.cursor = self.input.len();
        }
    }

    fn previous_command(&mut self) {
        if !self.history.is_empty() {
            let index = self
                .history_index
                .unwrap_or(self.history.len())
                .saturating_sub(1);
            self.history_index = Some(index);
            self.input.clone_from(&self.history[index]);
            self.cursor = self.input.len();
        }
    }

    fn next_command(&mut self) {
        if let Some(index) = self.history_index {
            if index + 1 < self.history.len() {
                self.history_index = Some(index + 1);
                self.input.clone_from(&self.history[index + 1]);
                self.cursor = self.input.len();
            } else {
                self.history_index = None;
                self.input.clear();
                self.cursor = 0;
            }
        }
    }
}

impl Drop for Tui {
    fn drop(&mut self) {
        let _ = ratatui::try_restore();
    }
}

#[allow(clippy::too_many_arguments)]
fn render(
    frame: &mut Frame,
    rx_frames: &VecDeque<Traffic>,
    tx_frames: &VecDeque<Traffic>,
    logs: &VecDeque<(LogLevel, String)>,
    input: &str,
    cursor: usize,
    suggestions: &str,
    config: &str,
    show_timestamps: bool,
    rx_total: u64,
    tx_total: u64,
    rx_rate: usize,
    tx_rate: usize,
    status: &Status<'_>,
) {
    let [traffic_area, logs_area, input_area, status_area] = Layout::vertical([
        Constraint::Fill(3),
        Constraint::Fill(2),
        Constraint::Length(4),
        Constraint::Length(2),
    ])
    .areas(frame.area());
    let [rx_area, tx_area] =
        Layout::horizontal([Constraint::Fill(1), Constraint::Fill(1)]).areas(traffic_area);

    render_traffic(
        frame,
        rx_area,
        " RX ",
        Color::Cyan,
        rx_frames,
        show_timestamps,
    );
    render_traffic(
        frame,
        tx_area,
        " TX ",
        Color::Magenta,
        tx_frames,
        show_timestamps,
    );

    let log_rows = logs_area.height.saturating_sub(2) as usize;
    let log_lines = logs
        .iter()
        .skip(logs.len().saturating_sub(log_rows))
        .map(|(level, text)| {
            let (label, color) = level_style(*level);
            Line::from(vec![
                Span::styled(
                    format!("{label:<5} "),
                    Style::new().fg(color).add_modifier(Modifier::BOLD),
                ),
                Span::raw(text.clone()),
            ])
        })
        .collect::<Vec<_>>();
    frame.render_widget(
        Paragraph::new(log_lines).block(Block::new().borders(Borders::ALL).title(" Information ")),
        logs_area,
    );

    frame.render_widget(
        Paragraph::new(vec![
            Line::raw(input),
            Line::styled(suggestions, Style::new().fg(Color::DarkGray)),
        ])
        .scroll((0, input_scroll(input, cursor, input_area)))
        .block(
            Block::new()
                .borders(Borders::ALL)
                .border_style(Style::new().fg(Color::Cyan))
                .title(" Command  [Tab complete · ↑↓ history] "),
        ),
        input_area,
    );
    let scroll = input_scroll(input, cursor, input_area);
    let cursor_width = UnicodeWidthStr::width(&input[..cursor]) as u16;
    let cursor_x = input_area
        .x
        .saturating_add(1)
        .saturating_add(cursor_width.saturating_sub(scroll))
        .min(input_area.right().saturating_sub(2));
    frame.set_cursor_position((cursor_x, input_area.y + 1));

    let connection = if status.connected {
        "CONNECTED"
    } else {
        "DISCONNECTED"
    };
    let run = if status.running { "RUNNING" } else { "STOPPED" };
    let rx = if status.receive_enabled {
        "RX ON"
    } else {
        "RX OFF"
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(connection, Style::new().fg(if status.connected { Color::Green } else { Color::Red }).add_modifier(Modifier::BOLD)),
                Span::raw(format!(
                    "  {run}  MODE {}  {rx}  RX {rx_total} ({rx_rate}/s)  TX {tx_total} ({tx_rate}/s)  USB {}",
                    status.mode, status.serial
                )),
            ]),
            Line::styled(config, Style::new().fg(Color::DarkGray)),
        ]),
        status_area,
    );
}

fn render_traffic(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    color: Color,
    values: &VecDeque<Traffic>,
    show_timestamps: bool,
) {
    let rows = area.height.saturating_sub(2) as usize;
    let lines = values
        .iter()
        .skip(values.len().saturating_sub(rows))
        .map(|traffic| {
            let timestamp = if show_timestamps {
                format!("{}  ", traffic.timestamp)
            } else {
                String::new()
            };
            Line::from(vec![
                Span::styled(timestamp, Style::new().fg(Color::DarkGray)),
                Span::styled(traffic.value.clone(), Style::new().fg(color)),
            ])
        })
        .collect::<Vec<_>>();
    frame.render_widget(
        Paragraph::new(lines).block(Block::new().borders(Borders::ALL).title(title)),
        area,
    );
}

fn command_matches(input: &str) -> Vec<&'static (&'static str, &'static str)> {
    if !input.starts_with('/') || input.contains(' ') {
        return Vec::new();
    }
    COMMANDS
        .iter()
        .filter(|(command, _)| command.starts_with(input))
        .collect()
}

fn suggestions(input: &str) -> String {
    match input {
        "/rx " | "/timestamp " => return "on   off".into(),
        "/status-format " => return "human   raw".into(),
        _ => {}
    }
    command_matches(input)
        .into_iter()
        .take(6)
        .map(|(command, description)| format!("{command} — {description}"))
        .collect::<Vec<_>>()
        .join("   ")
}

fn config_line(config: &Map<String, Value>, raw: bool) -> String {
    if config.is_empty() {
        return "CONFIG loading...".into();
    }
    if raw {
        return [
            ("CAN", "can_speed"),
            ("FD", "fd_speed"),
            ("TERM", "term_res"),
            ("STD", "standard"),
            ("AUTO", "auto_retrans"),
            ("BUSOFF", "busoff_recovery"),
            ("ID", "id"),
        ]
        .into_iter()
        .filter_map(|(label, key)| {
            config
                .get(key)?
                .as_str()
                .map(|value| format!("{label} {value}"))
        })
        .collect::<Vec<_>>()
        .join("  ");
    }
    let value = |key| config.get(key).and_then(Value::as_str).and_then(hex_bytes);
    let preset = |key| {
        value(key)
            .and_then(|bytes| bytes.first().copied())
            .map(|number| format!("preset #{number}"))
            .unwrap_or_else(|| "unknown".into())
    };
    let flag = |key| {
        value(key)
            .and_then(|bytes| bytes.first().copied())
            .map(|value| if value == 0 { "off" } else { "on" })
            .unwrap_or("unknown")
    };
    let id = value("id")
        .filter(|bytes| bytes.len() >= 2)
        .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]).to_string())
        .unwrap_or_else(|| "unknown".into());
    format!(
        "Nominal {}  Data {}  Terminator {}  FD-standard {}  Auto-retry {}  Bus-off recovery {}  Device ID {}",
        preset("can_speed"),
        preset("fd_speed"),
        flag("term_res"),
        flag("standard"),
        flag("auto_retrans"),
        flag("busoff_recovery"),
        id
    )
}

fn hex_bytes(value: &str) -> Option<Vec<u8>> {
    let compact = value
        .chars()
        .filter(|character| character.is_ascii_hexdigit())
        .collect::<String>();
    if compact.len() % 2 != 0 {
        return None;
    }
    (0..compact.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&compact[index..index + 2], 16).ok())
        .collect()
}

fn log_text(value: &Value) -> (LogLevel, String) {
    if let Some(event) = value.get("event").and_then(Value::as_str) {
        return match event {
            "session_started" => (LogLevel::Debug, String::new()),
            "connected" => (
                LogLevel::Info,
                format!(
                    "Connected in {} mode",
                    value["mode"].as_str().unwrap_or("?")
                ),
            ),
            "reconnecting" => (
                LogLevel::Warn,
                format!("Reconnecting: {}", value["error"].as_str().unwrap_or("")),
            ),
            "disconnected" => (
                LogLevel::Warn,
                format!("Disconnected: {}", value["error"].as_str().unwrap_or("")),
            ),
            "session_stopping" => (LogLevel::Info, "Session stopping".into()),
            _ => (LogLevel::Debug, format!("Event: {event}")),
        };
    }
    if value["ok"] == false {
        return (
            LogLevel::Error,
            value["error"].as_str().unwrap_or("unknown").to_string(),
        );
    }
    match value["op"].as_str().unwrap_or("") {
        "help" => (
            LogLevel::Info,
            value["data"]["text"].as_str().unwrap_or("").into(),
        ),
        "send" => (LogLevel::Info, "TX accepted by USB adapter".into()),
        "config_get" => (LogLevel::Debug, "Configuration refreshed".into()),
        "config_set" => (LogLevel::Info, "Configuration updated and read back".into()),
        op if !op.is_empty() => (LogLevel::Debug, format!("{op}: {}", value["data"])),
        _ => (LogLevel::Debug, value.to_string()),
    }
}

fn level_style(level: LogLevel) -> (&'static str, Color) {
    match level {
        LogLevel::Debug => ("DEBUG", Color::DarkGray),
        LogLevel::Info => ("INFO", Color::Green),
        LogLevel::Warn => ("WARN", Color::Yellow),
        LogLevel::Error => ("ERROR", Color::Red),
    }
}

fn on_off(value: &str) -> Option<bool> {
    match value {
        "on" | "true" | "1" => Some(true),
        "off" | "false" | "0" => Some(false),
        _ => None,
    }
}

fn system_timestamp() -> String {
    Local::now().format("%H:%M:%S%.3f").to_string()
}

fn previous_boundary(value: &str, cursor: usize) -> Option<usize> {
    value[..cursor]
        .char_indices()
        .next_back()
        .map(|(index, _)| index)
}

fn next_boundary(value: &str, cursor: usize) -> usize {
    value[cursor..]
        .chars()
        .next()
        .map(|character| cursor + character.len_utf8())
        .unwrap_or(value.len())
}

fn input_scroll(input: &str, cursor: usize, area: Rect) -> u16 {
    let width = area.width.saturating_sub(3);
    (UnicodeWidthStr::width(&input[..cursor]) as u16).saturating_sub(width)
}

fn trim_rate(values: &mut VecDeque<Instant>, now: Instant) {
    while values
        .front()
        .is_some_and(|value| now.duration_since(*value) >= Duration::from_secs(1))
    {
        values.pop_front();
    }
}

fn trim<T>(values: &mut VecDeque<T>, maximum: usize) {
    while values.len() > maximum {
        values.pop_front();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn formats_human_status_and_command_matches() {
        let config = json!({
            "can_speed":"0C", "fd_speed":"06", "term_res":"00", "standard":"01",
            "auto_retrans":"01", "busoff_recovery":"00", "id":"3412"
        });
        let human = config_line(config.as_object().unwrap(), false);
        assert!(human.contains("Nominal preset #12"));
        assert!(human.contains("Device ID 4660"));
        assert!(suggestions("/sta").contains("/status"));
        assert_eq!(
            level_style(log_text(&json!({"ok":false,"error":"bad"})).0).0,
            "ERROR"
        );
        assert_eq!(previous_boundary("a中b", 4), Some(1));
        assert_eq!(next_boundary("a中b", 1), 4);
        assert_eq!(system_timestamp().len(), 12);
    }
}
