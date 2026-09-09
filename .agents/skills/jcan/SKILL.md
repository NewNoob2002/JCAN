---
name: jcan
description: Operate JooDevice JTool-CAN USB adapters through this repository's Rust jcan CLI for discovery, configuration, persistent JSONL sessions, internal loopback, bounded capture, CAN/CAN-FD traffic, and CANopen reads. Do not use the legacy JCAN MCP server.
---

# JCAN CLI

Use target/release/jcan --json; build it with cargo build --release when needed. Follow the repository AGENTS.md for authorization, bounds, cleanup, and exact command examples.

Start with self-test, then scan, select an explicit serial, and read config-get. Device discovery is never authorization for writes, reboot, SDO, or physical transmission.

For repeated Agent operations, run target/release/jcan --json --serial SERIAL session --mode MODE and exchange one JSON object per stdin/stdout line. For a human terminal, omit --json to open the full-screen TUI, then use /help, /status, /config, /config set, /send, /rx, /timestamp, /status-format, and /quit. RX/TX panes, colored logs, command completion, and the status line update independently. The session owns one USB handle, reconnects by serial, and stops on shutdown, EOF, or Ctrl-C. It never retries a send request.

Use the smallest bounded operation that proves the request. Stop on non-zero exit, ok=false, timeout, or cleanup errors. Do not retry physical operations automatically. Bootloader entry is unsupported.
