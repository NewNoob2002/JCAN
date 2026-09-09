# JCAN CLI 1.0

Native Rust command-line client for JooDevice/JTool-CAN USB adapters (ffff:0004). It replaces the Python MCP control path for direct human and Agent use.

The former Python/MCP implementation is archived under legacy/python-mcp for historical reference and is not a supported hardware control path.

## Build

    cargo build --release
    target/release/jcan --version
    target/release/jcan --json self-test

The only system requirement is libusb 1.0. On Linux, install 99-jcan.rules when USB access is denied, then reconnect the adapter.

## Safe first run

    target/release/jcan --json scan
    target/release/jcan --json --serial SERIAL config-get
    target/release/jcan --json --serial SERIAL loopback-test

Every hardware command requires an explicit serial number. Use --json for machine-readable JSONL output and normal output for interactive terminal use.

## Interactive session

Run it directly in a terminal. Received frames are hidden by default, so they do not take over the prompt:

    target/release/jcan --serial SERIAL session --mode silent

The terminal switches to a full-screen layout. RX/TX traffic, logs, command editing, and the live configuration StatusLine are rendered independently:

    ┌ RX traffic ─────────┬ TX traffic ──────┐
    │ 12:30:01.204 701 7F │ 12:30:02.310 123… │
    ├ Information ───────────────────────────┤
    │ Connected in silent mode               │
    ├ Command ───────────────────────────────┤
    │ /status                                │
    └────────────────────────────────────────┘
    CONNECTED  RUNNING  MODE silent  RX ON  RX 120 (10/s)  TX 3 (0/s)
    Nominal preset #12  Data preset #6  Terminator off  Device ID 0

Then use slash commands:

    /status
    /config
    /config set terminal-resistance 1
    /send 123 11 22 33 44
    /send 601#2B17100000000000
    /rx on
    /rx off
    /timestamp on
    /timestamp off
    /status-format human
    /status-format raw
    /mode silent
    /quit

Configuration changes stop CAN, write with readback and rollback on failure, then restore the current mode. Use /help for the full command list. Add --receive only when received frames should be shown immediately.

Traffic timestamps use the system local time with millisecond precision. Enter submits a command; Left/Right, Home/End, Backspace/Delete edit at the cursor; Up/Down recalls history; Tab completes a unique command; Esc clears the editor; Ctrl-C performs the same clean shutdown as /quit. Long commands scroll horizontally. Typing / shows matching commands and descriptions. RX/TX totals and one-second frame rates continue updating when RX display is hidden.

The TUI redraws only when needed, caps active rendering at roughly 30 FPS, and drops to a low idle refresh rate while CAN reception and statistics continue independently.

## Agent JSONL session

Keep one USB handle open and exchange JSONL over stdin/stdout:

    target/release/jcan --json --serial SERIAL session --mode silent --reconnect-ms 2000

Example requests, one JSON object per line:

    {"id":1,"op":"ping"}
    {"id":2,"op":"status"}
    {"id":3,"op":"config_get"}
    {"id":4,"op":"config_set","field":"terminal-resistance","value":1}
    {"id":5,"op":"receive","enabled":true}
    {"id":6,"op":"send","can_id":"123","data_hex":"11 22 33 44"}
    {"id":6,"op":"set_mode","mode":"silent"}
    {"id":7,"op":"disconnect"}
    {"id":8,"op":"connect"}
    {"id":9,"op":"shutdown"}

Responses echo id. Connection changes are emitted as event objects. Received frames are emitted only after receive is enabled or when session starts with --receive. A numeric can_id is decimal; a string can_id is hexadecimal. The session reconnects the selected serial automatically, but never retries a send command. EOF, shutdown, and Ctrl-C stop CAN and release USB.

Human CLI and TUI send commands accept both separated bytes and Linux candump-style ID#DATA, for example send 601#2B17100000000000. Agent JSONL continues using separate can_id and data_hex fields.

One-shot CLI send keeps CAN running for 1000 ms after USB acceptance before CANStop. This is the physically validated default for this adapter; --settle-ms can override it. The result still reports usb_command_accepted because only an independent receiver can prove bus delivery. For repeated low-latency sends, use the persistent session so CAN remains open instead of repeatedly starting and stopping it.

## Commands

- self-test and scan
- config-get, config-set, and reversible config-test
- send and bounded capture
- bounded periodic transmission with jitter statistics
- internal loopback test and benchmark
- expedited CANopen sdo-read
- CANopen identity and CiA 402 status helpers
- CAN start, stop, and reboot with re-enumeration verification
- persistent JSONL session with automatic reconnect

Run target/release/jcan help for exact syntax. Bootloader entry is intentionally excluded. Ctrl-C during capture or periodic transmission performs CANStop before exit.

Agent authorization and cleanup rules are defined in AGENTS.md.
