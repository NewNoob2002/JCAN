# JCAN 1.0 Agent CLI policy

Use the Rust CLI in this repository for JooDevice/JTool-CAN adapters. Do not use or start the legacy JCAN MCP server, and do not call jcan.py or jcan_mcp.py for hardware operations.

## Build and discovery

1. Build with cargo build --release when target/release/jcan is missing or stale.
2. Run target/release/jcan --json self-test before hardware work.
3. Run target/release/jcan --json scan and select an explicit non-empty USB serial number.
4. Run target/release/jcan --json --serial SERIAL config-get to record the baseline before any further hardware operation.

Use --json for Agent calls. Each output line is a JSON object. A non-zero exit status or an ok=false result is a stop condition.

## Risk boundaries

- Read-only: self-test, scan, config-get.
- Internal-only: bounded loopback-test and loopback-benchmark; these do not drive the external CAN bus.
- Passive bus: capture --mode silent with explicit --duration-ms and --max-frames.
- State-changing: config-set, send, periodic, start, stop, sdo-read, and reboot require current user authorization covering the exact device and operation. Physical transmission authorization must include ID, frame flags, DLC/payload, and count/timing where applicable.

Discovery is not authorization. Do not infer permission from a connected adapter, an existing configuration, prior evidence, or an old bus profile. No profile or ID allowlist is required; protocol-valid IDs and payloads are accepted when the user has authorized them.

## Operation rules

- Always pass --serial SERIAL; never rely on the first enumerated device.
- A single-send request means one send process invocation, without retry.
- One-shot send defaults to --settle-ms 1000 before CANStop because zero-delay stop has lost physical frames on this adapter. Do not describe USB acceptance as proven bus delivery.
- Keep capture within 1..600000 ms and 1..100000 frames.
- Keep periodic transmission within 10..60000 ms and 1..1500 sends. Never turn a single send into periodic traffic.
- config-set accepts only documented named fields and performs baseline read, write, readback, and rollback on failure.
- Ctrl-C is supported for capture and periodic commands; allow the process to finish CANStop and USB cleanup.
- Stop on errors, timeouts, cleanup failures, or malformed JSON. Do not silently retry a physical operation.
- Report USB command acceptance separately from independently verified physical delivery.
- Bootloader entry is intentionally unavailable in the Rust 1.0 CLI.

## Persistent session

Use target/release/jcan --json --serial SERIAL session --mode MODE when several operations must share one USB connection. Starting a session starts CAN in the selected mode and therefore requires authorization for that device and mode. Prefer silent mode for observation.

When a human runs session without --json in a real terminal, it opens a full-screen TUI with separate RX/TX traffic panes, severity-colored logs, command completion, and a configuration/status line with totals and one-second frame rates. Agent sessions must pass --json and continue using JSONL.

Write one JSON request per stdin line. Supported operations are ping, status, config_get, config_set, receive, send, start, stop, set_mode, disconnect, connect, and shutdown. Responses echo the request id; asynchronous connection and frame messages contain an event field. Frame output is disabled by default; enable it with {"op":"receive","enabled":true} or --receive.

The session automatically retries connection by serial number. It never retries a send request. On an operation error, inspect the response and connection events before continuing. Send shutdown or close stdin and wait for process exit so CANStop and USB cleanup complete.

## Common commands

    target/release/jcan --json self-test
    target/release/jcan --json scan
    target/release/jcan --json --serial SERIAL config-get
    target/release/jcan --json --serial SERIAL capture --mode silent --duration-ms 1000 --max-frames 100
    target/release/jcan --json --serial SERIAL loopback-test
    target/release/jcan --json --serial SERIAL send 123 11 22 33 44
    target/release/jcan --json --serial SERIAL periodic 123 100 10 11 22 33 44
    target/release/jcan --json --serial SERIAL sdo-read 01 1018 01
    target/release/jcan --json --serial SERIAL session --mode silent --reconnect-ms 2000

Save durable validation output under evidence/ when the task requires an audit trail. Record the exact command, exit status, stdout, stderr, adapter serial, and whether delivery was independently observed.
