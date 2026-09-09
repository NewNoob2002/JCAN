# JCAN CLI contract

The supported control surface is target/release/jcan. Do not use the legacy MCP tools.

One-shot Agent calls use --json and emit objects containing ok, operation, data, and warnings. Capture emits frame objects followed by a summary object. Errors exit non-zero and emit ok=false to stderr.

Persistent Agent calls use target/release/jcan --json --serial SERIAL session --mode MODE. Write one JSON object per stdin line. Supported operations are ping, status, config_get, config_set, receive, send, start, stop, set_mode, disconnect, connect, and shutdown. Responses echo id; asynchronous messages contain event. Frame events are disabled by default. Human terminals omit --json to use the TUI.

Run target/release/jcan help for complete syntax. Hardware commands require an explicit serial; capture and periodic commands enforce finite limits.
