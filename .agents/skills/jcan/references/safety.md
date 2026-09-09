# JCAN CLI safety

Scanning does not authorize configuration changes, reboot, CAN transmission, SDO traffic, or periodic traffic. Use an explicit serial for every hardware command and obtain current authorization for state-changing or physical-bus operations.

Single send means one invocation and no retry. One-shot CLI send defaults to a 1000 ms hold before CANStop; USB acceptance is not proof of bus delivery. Capture and periodic traffic must use exact finite bounds. On Ctrl-C, wait for CANStop and USB cleanup. Stop on every non-zero exit, ok=false, timeout, malformed response, or cleanup error.

The CLI exposes named configuration fields only and excludes bootloader entry. No bus profile is required or acts as an allowlist.
