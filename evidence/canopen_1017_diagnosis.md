# JCAN 0x601 / 0x1017:00 diagnosis — 2026-09-07

## Findings

- The user confirms the current heartbeat and value 1000 were configured manually through the RK3588. They are not evidence of a successful JCAN write.
- Three recorded `send_once` calls used standard Classic CAN ID 1537 (0x601), DLC 8, data `2B 17 10 00 E8 03 00 00`, with all flags false, and returned `ok=true`. Subsequent uploads returned zero. See runtime send records at 03:31:40, 03:43:37, 03:54:31 UTC and upload records at 03:31:59, 03:44:23, 03:55:33 UTC.
- Current adapter serial is `207F346D5650`. Configuration read succeeded and matched the recorded raw baseline. Passive capture received six `0x701 / 7F` frames in 3.001 seconds. An independent JCAN upload returned `4B 17 10 00 E8 03 00 00`, value 1000. This establishes that some bidirectional JCAN traffic works, not that every transmission works.
- The one same-value SDO diagnostic returned `ok=false`, `等待 CAN ID 581 超时`. No further physical operations were attempted. The tool discards partial transaction progress on failure, so the failed phase (initial upload, download response, or verification upload) and whether the download was issued are unknown. Cleanup is attempted by `finally: can.stop()` and the context manager; successful physical cleanup was not independently verified.

## Encoding and send lifecycle

Vendor `x64/jtool.dll` export `CANSend` is RVA 0x4840. The adjacent saved disassembly shows four one-byte flags at payload offsets 0..3, a little-endian 32-bit ID at offset 4, byte length at offset 8, and data at offset 9 padded to 64 bytes. The passed payload length is 0x49 (73). This matches `JCan.send` and the exact tested request:

```text
USB header: 4A 05 00 00 49 00
Payload:    00 00 00 00 01 06 00 00 08 2B 17 10 00 E8 03 00 00
            followed by 56 zero bytes
```

The offline exact-byte assertion and MCP self-test passed. No encoding discrepancy was found for this specific frame. These tests do not establish physical bus delivery.

`_send_once` executes `start(normal) -> send -> stop -> close`. `send` checks the adapter USB command response, and `_send_once` returns the requested frame. It does not enable reception, wait for an SDO response, read back the object, or obtain a separate TX-complete event. The DLL likewise checks a one-byte adapter response; the available host code does not establish whether that response means queued or physically transmitted. Premature CANStop remains a hypothesis, not a confirmed cause. A fixed sleep would not prove delivery and has not been added.

## Evidence

- Baseline: `runtime/20260907T042006.827007Z-1788754806827026143-get_config.json`
- Passive capture: `runtime/20260907T042047.091111Z-1788754847091139268-capture.json`
- Current object read: `runtime/20260907T042104.183284Z-1788754864183310143-sdo_read.json`
- Failed diagnostic: `runtime/20260907T042153.272913Z-1788754913272950909-sdo_u16_same_value_test.json`
- Host self-test: `runtime/20260907T042251.162207Z-1788754971162238161-self_test.json`
- Vendor disassembly: `canopen_1017_vendor_can_disassembly.txt`
- Diagnostic preflight: `canopen_1017_diagnosis_preflight.json`

## Next discriminating observation

Start an independent RK3588 capture before a newly authorized single-frame retry. Record outgoing 0x601 and incoming 0x581, not only heartbeat frames. No 0x601 points toward transmission/start/stop or bus delivery; a correct 0x601 with an abort or success response points toward object handling or JCAN response reception. Distinguishing early stop from other TX failures additionally requires a bounded comparison that keeps the adapter running after send. Do not classify a missing response as proof that the request never reached the driver.
