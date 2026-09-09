# Rust JCAN CLI 1.0 release evidence

Date: 2026-09-09

## Release checks

- `cargo fmt --all -- --check`: passed
- `cargo test`: passed (library, CLI/session/TUI, and JSONL integration tests)
- `cargo clippy --all-targets -- -D warnings`: passed
- `cargo build --release`: passed
- `target/release/jcan --json self-test`: passed
- JSONL session startup, ping, status, and shutdown without hardware: passed
- TUI pseudo-terminal command entry and terminal restoration: passed
- User independently confirmed physical delivery after the one-shot send default was changed to a 1000 ms hold before CANStop.

## Supported release surface

- Rust one-shot CLI with JSON output for Agents
- Persistent JSONL session for long-lived Agent use
- Full-screen Ratatui interface for human use
- Explicit USB serial selection, bounded capture/periodic operations, cleanup, configuration readback/rollback, and no bootloader entry

## Legacy archive

The former Python/MCP implementation is preserved under `legacy/python-mcp/`. Its historical evidence remains under `evidence/`; it is not the supported hardware interface.
