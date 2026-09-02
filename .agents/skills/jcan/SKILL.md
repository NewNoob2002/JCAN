---
name: jcan
description: Operate JooDevice JTool-CAN USB adapters through this repository's JCAN MCP server for discovery, configuration, internal loopback, profile-gated capture, CAN or CANopen traffic, and bounded periodic tests. Use only for JCAN/JTool-CAN hardware, not generic SocketCAN interfaces or unrelated CAN adapters.
---

# JCAN

Use the JCAN MCP as the control plane; do not reproduce USB protocol operations in shell commands or call the MCP once per periodic frame.

## Workflow

1. Use `jcan_scan`, select an explicit USB serial number, then use `jcan_get_config` to record the baseline. Device discovery is never authorization for a write, reboot, or physical CAN transmission.
2. Choose the least-risk tool that proves the request:
   - No hardware: `jcan_self_test`.
   - Read-only: `jcan_scan`, `jcan_get_config`, `jcan_bus_profile_status`.
   - Bounded internal CAN: `jcan_loopback_test` or `jcan_loopback_benchmark`; these do not drive the external bus.
   - Physical capture or traffic: use only the profile-gated tools and exact serial/frame/object requested by the user.
   - Configuration writes or reboot: require explicit current authorization and a recovery plan.
3. Before configuration, reboot, SDO write, single-frame TX, or periodic TX, read [references/safety.md](references/safety.md). Read [references/tool-contract.md](references/tool-contract.md) when selecting a tool or interpreting its structured result.
4. Treat `ok=false`, warnings, profile/config mismatches, timeout, or cleanup errors as a stop condition. Preserve the returned `evidence_path`; do not silently retry a physical operation.

Do not expose `IntoBoot`, arbitrary configuration keys, unbounded capture, or unbounded periodic transmission. Do not change a frame from `periodic=false` to `true` without a new explicit user request.
