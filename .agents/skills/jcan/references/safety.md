# JCAN safety boundary

Use the smallest operation that can answer the request. Scanning or detecting an adapter does not authorize configuration changes, reboot, CAN transmission, SDO download, periodic traffic, or bootloader entry.

## Risk levels

- Read-only: self-test, scan, configuration read, and bus-profile status.
- Internal-only: silent-loopback and its bounded benchmark. They access the adapter but do not drive the physical bus.
- Passive physical bus: bounded `jcan_capture` in silent mode. Confirm the serial, bitrate/profile match, duration, and frame limit.
- State-changing: configuration writes, reboot, `jcan_send_once`, CANopen same-value SDO test, and periodic start. These require explicit current authorization for the exact target and bounds.

Before a state-changing operation, record a safety preflight with the adapter serial, transport, exact configuration or CAN stimulus, limits, and cleanup/recovery path. For physical TX, confirm the profile is enabled and approved and that ID, standard/extended flag, CAN/CAN-FD flags, DLC, and payload are explicitly allowed. Never infer permission from a matching profile alone.

Always keep waits and frame counts bounded. On failure, stop CAN and close USB; do not retry unless the user authorizes another attempt after the failure is classified. Periodic tasks must be stopped explicitly when no longer needed, and MCP shutdown must be allowed to complete cleanup.

`IntoBoot` is outside the supported MCP surface. Bus-Off fault injection remains deferred until suitable external equipment and a separate authorization/preflight are available.
