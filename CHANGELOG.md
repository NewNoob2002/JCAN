# Changelog

## 1.0.0 - 2026-09-09

- Replaced the legacy Python/MCP control path with a native Rust CLI.
- Added discovery, configuration, CAN/CAN-FD send and capture, bounded periodic traffic, loopback tests, CANopen reads, and adapter lifecycle commands.
- Added persistent JSONL sessions for Agents with serial-based reconnect and explicit shutdown cleanup.
- Added a full-screen Ratatui interface with separate RX/TX panes, colored logs, command completion and editing, local timestamps, readable configuration, counters, and frame rates.
- Added Linux candump-style `ID#DATA` send syntax.
- Made one-shot sends hold CAN for 1000 ms before CANStop to avoid losing queued physical frames.
- Added Agent policy and local skill documentation for the Rust interface.

The Python MCP implementation and its evidence remain in the repository as historical reference. They are not the supported 1.0 control surface.
