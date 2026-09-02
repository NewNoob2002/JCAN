# JCAN Linux API 解析记录

解析对象：`x64/jtool.h`、`x64/jtool.dll`、`x64/jtool.exe`（jtool 1.2.0.0）。

## 结论

- DLL 共导出 96 个函数，其中 JCAN 相关 17 个：5 个收发控制接口和 12 个配置/维护接口。
- 导出表、头文件、命令行工具中均未发现厂商提供的设备端定时/周期发送接口；不能排除固件存在未使用的私有命令。现有能力下，周期发送应由 Linux 上位机按绝对单调时钟调度，并重复调用单帧发送。
- `CANSend` 是同步单帧接口；自动重传仅表示 CAN 控制器在未收到 ACK/仲裁失败时重试同一帧，不等于周期发送。

## USB 协议

命令包头为 6 字节小端结构：

```text
magic:u8=0x4A, channel:u8, response:u8, command:u8, payload_length:u16le
```

- CAN 运行通道：`channel=5`；`command=0` 发送、`1` 启动、`2` 停止。
- 接收使能：`channel=0, command=4`；CAN 数据从 bulk-in `0x83` 返回。
- 配置读取：`channel=0, command=1`，payload 为以 NUL 结尾的 ASCII 配置键。
- 配置写入：`channel=0, command=2`，payload 为 `配置键 + NUL + 二进制值`。
- 重启：`channel=0, command=3`，空 payload。
- Linux CLI 可用 `config-dump` 只读获取当前配置原始值，便于写入前备份和写后核对。
- `config-roundtrip-test` 会备份配置，验证可恢复的配置写入/回读，并在退出前恢复完整基线；不执行重启或进入 Bootloader。
- `loopback-benchmark` 在内部静默回环模式测量同步单帧往返吞吐和延迟，不代表物理 CAN 总线时延。

## 12 个配置/维护接口

| DLL API | 配置键/命令 | 值编码 | Python CLI |
|---|---|---|---|
| `JCANSetNominalSpeed` | `can_speed`, `can_customval` | 预置编号 `u8`；清除自定义使能 `u16` | `set-nominal-speed` |
| `JCANSetDataSpeed` | `fd_speed`, `fd_customval` | 预置编号 `u8`；清除自定义使能 `u16` | `set-data-speed` |
| `JCANSetNominalCustomSpeed` | `can_customval` | `<enable,prescaler,sjw,seg1,seg2>`，5 个 `u16le` | `set-nominal-custom` |
| `JCANSetDataCustomSpeed` | `fd_customval` | 同上 | `set-data-custom` |
| `JCANSetFDStandard` | `standard` | `u8`，0/1 | `set-fd-standard` |
| `JCANSetTerminalResistance` | `term_res` | `u8`，0/1 | `set-terminal-resistance` |
| `JCANSetBusOffAutoRecovery` | `busoff_recovery` | `u8`，0/1 | `set-busoff-recovery` |
| `JCANSetAutoRetransmission` | `auto_retrans` | `u8`，0/1 | `set-auto-retransmission` |
| `JCANReboot` | 命令 3 | 无 | `reboot` |
| `JCANSetHardVersion` | `hardware_version` | 10 字节 NUL 填充，最多 9 个 ASCII 字符 | `set-hardware-version` |
| `JCANSetID` | `id` | `u16le` | `set-device-id` |
| `JCANIntoBoot` | `stop_in_boot` 后重启 | `u8=1` | `into-boot` |

自定义经典 CAN 时序范围：prescaler 1..512、sjw 1..128、seg1 2..256、seg2 2..128，且 sjw <= seg2。

自定义 CAN FD 数据相位范围：prescaler 1..32、sjw 1..16、seg1 1..32、seg2 1..16，且 sjw <= seg2。

预置速率编号到实际 bit/s 的映射没有出现在头文件或导出接口中，真机确认前不要猜测。上位机应优先暴露明确的 bit timing 参数，或在抓取 Windows 工具 USB 流量后补全映射表。

## 周期发送建议

Linux 端使用 `time.monotonic_ns()`/`clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, ...)` 维护下一次绝对截止时间；每次到期调用 `CANSend` 等价的单帧 USB 命令。不要用“发送后 sleep(period)”累积漂移。

最小任务模型只需：帧定义、周期、下次截止时间、启停状态。多帧时用一个调度线程和按截止时间排序的队列即可；只有测得吞吐不足后再增加工作线程。

## 验证状态

- `python3 jcan.py self-test`：通过；覆盖 12 个配置/维护接口的 USB 封包、配置读取回复解析、时序范围检查，以及原有收发解析。
- `python3 -m py_compile jcan.py`：通过。
- 2026-09-01 真机：适配器 `ffff:0004`、序列号 `207F346D5650`；标准 CAN、扩展 CAN、CAN FD+BRS 内部静默回环通过。
- 10 项配置读取、写入、回读和完整恢复通过；普通重启后设备以相同序列号重新枚举，重启后回环通过。
- 100 帧同步内部回环：2179.0 fps；延迟 min/avg/P95/max = 0.414/0.457/0.505/0.707 ms。
- 未验证：`JCANIntoBoot`、物理收发器/线束/终端/真实总线位时序，以及设备端是否存在未公开的周期发送私有命令。
- 2026-09-02 MCP 阶段 3 HIL：具名配置写入回读、完整配置往返恢复和普通重启重枚举通过；测试后十项配置逐字节等于基线。
- 2026-09-02 阶段 4 初始 host 验证：默认拒绝 TOML profile、有界 silent 抓包和精确白名单单帧发送通过；当时物理参数尚未填写。
- 2026-09-02 阶段 4 profile 已按 500 kbit/s、CAN 2.0B 扩展 TX、ID 0x7FF、DLC 4、payload 00000000 激活；真机配置只读匹配，尚未发送物理帧。
