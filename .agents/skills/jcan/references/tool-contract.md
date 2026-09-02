# JCAN MCP tool contract

All hardware operations require an explicit adapter `serial`. Results are structured records with `ok`, `operation`, `data`, `warnings`, and `evidence_path`. An MCP protocol success with `ok=false` is an operation failure.

| Intent | Tool | Required boundary |
|---|---|---|
| Verify host protocol code | `jcan_self_test` | No hardware |
| Find adapters | `jcan_scan` | Read-only; select one serial before continuing |
| Read baseline | `jcan_get_config` | Exact serial |
| Inspect physical authorization | `jcan_bus_profile_status` | No USB access; do not treat approval as user permission |
| Internal functional check | `jcan_loopback_test` | Three bounded internal frames |
| Internal performance check | `jcan_loopback_benchmark` | Explicit bounded count |
| Passive physical capture | `jcan_capture` | Profile match plus bounded `duration_ms` and `max_frames` |
| Send one physical frame | `jcan_send_once` | Exact profile-approved serial, ID, flags, DLC, and payload |
| Read CANopen object | `jcan_sdo_read` | Profile-approved node/index/subindex |
| Same-value U16 node test | `jcan_sdo_u16_same_value_test` | Explicit write authorization; writes only the value just read and does not save EEPROM |
| Start periodic traffic | `jcan_periodic_start` | Exact profile rule with `periodic=true`, bounded period and count |
| Inspect/stop periodic traffic | `jcan_periodic_list`, `jcan_periodic_stop` | Exact serial and returned task ID |
| Reversible configuration test | `jcan_config_roundtrip_test` | Preflight; verify restoration |
| Apply configuration | `jcan_apply_config` | Named fields only, baseline/readback/rollback required |
| Reboot adapter | `jcan_reboot` | Separate authorization and re-enumeration timeout |

Periodic traffic owns the adapter session; other hardware operations should remain blocked until it completes or is stopped. Report actual counts, timing/jitter, cleanup state, warnings, and evidence paths instead of inferring success from request completion.
