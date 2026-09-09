# JCAN Linux API 解析记录

> Legacy Python/MCP reference. Rust CLI 1.0 is now the supported user and Agent interface; see README.md and AGENTS.md.

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

MCP 已实现 `jcan_periodic_start/list/stop`：使用一个持久 CAN 会话和单调绝对截止时间，丢期时跳过过期实例而不突发补发；任务限制为 1–1500 帧、10–60000 ms。MCP 不设置工程级 ID、帧格式或 payload 白名单，只仲裁 CAN/CAN FD 协议合法性；工程策略由调用方负责。

## 单帧发送结果与停止时序诊断

`jcan_send_once` 的 `ok=true` 仅表示适配器接受 USB 命令并完成主机调用，不能证明 CAN 总线交付或 CANopen 对象写入成功；结果用 `completion="usb_command_accepted"` 明确这一点。

可选 `settle_ms=0..2000` 在发送命令返回后保持 CAN 运行，再执行 CANStop 和 USB close。默认 0 保持原有时序；例如 100 可用于对照“立即停止是否影响发送”。该参数不重发、不接收应答，也不是 TX-complete 保证。应由独立抓包确认请求和应答，不能把延时视为已验证的修复。修改后重启 MCP 服务加载参数。

## CANopen 请求与反馈

`jcan_sdo_read` 在同一个 CAN 会话内启用接收、发送 upload 请求，并等待匹配节点、帧格式、index/subindex 的响应（响应等待最多 2 秒）。`jcan_sdo_u16_same_value_test` 执行读取 U16、同值 download、等待确认、再次读取验证；返回请求和响应报文，不保存 EEPROM。底层 `sdo_write` 支持 1–4 字节 expedited download 并等待反馈，但当前 MCP 未暴露任意值 SDO 写入工具。

这些是一次调用内的请求／应答事务。MCP 使用异步入口和 USB 工作线程，但同一硬件会话仍串行独占；没有长期后台 RX 订阅或原始报文的通用 send-and-wait 工具。`jcan_send_once` 不接收响应；周期工具也不把从机响应作为每次发送的成功条件。

2026-09-07 修复：SDO 在同一批 USB 帧中执行完整响应匹配，避免无关对象响应导致正确响应丢失；周期会话持有硬件锁直到 CANStop/USB close 完成；启动失败也尝试停止和关闭，保留清理错误。代码更新需由调用方重启 MCP 加载。

## 长时间采集

`jcan_capture` 支持 `duration_ms=1..600000`（最长 10 分钟）和 `max_frames=1..100000`，先达到任一上限即结束。默认仍为 1000 ms / 100 帧；长时间调用必须显式设置足够的帧数，例如 `duration_ms=60000, max_frames=100000`。所有帧保存在内存并随结果返回，达到帧数上限时仍会提前结束。

客户端超时应大于采集时长，并预留 USB 清理和结果传输时间。MCP 请求取消后，后台线程在当前 USB 读取返回后停止采集（每次读取超时最多 100 ms），执行 CANStop 并关闭 USB；CANStop/关闭本身仍需要时间。仅在客户端本地停止等待而未发送 MCP 取消请求时，服务端会继续采集到时长或帧数上限。修改后重启调用方管理的 JCAN MCP 服务以加载新代码。

## 验证状态

- `python3 jcan.py self-test`：通过；覆盖 12 个配置/维护接口的 USB 封包、配置读取回复解析、时序范围检查，以及原有收发解析。
- `python3 -m py_compile jcan.py`：通过。
- 2026-09-01 真机：适配器 `ffff:0004`、序列号 `207F346D5650`；标准 CAN、扩展 CAN、CAN FD+BRS 内部静默回环通过。
- 10 项配置读取、写入、回读和完整恢复通过；普通重启后设备以相同序列号重新枚举，重启后回环通过。
- 100 帧同步内部回环：2179.0 fps；延迟 min/avg/P95/max = 0.414/0.457/0.505/0.707 ms。
- 未验证：`JCANIntoBoot`、物理收发器/线束/终端/真实总线位时序，以及设备端是否存在未公开的周期发送私有命令。
- 2026-09-02 MCP 阶段 3 HIL：具名配置写入回读、完整配置往返恢复和普通重启重枚举通过；测试后十项配置逐字节等于基线。
- 2026-09-02 阶段 4 初始 host 验证：默认拒绝 TOML profile、有界 silent 抓包和精确白名单单帧发送通过；当时物理参数尚未填写。
- 2026-09-02 阶段 4 profile 已修正为 500 kbit/s 经典 CAN 标准 TX、ID 0x7FF、DLC 4、payload 00000000；一次 MCP 物理发送请求通过，尚无独立接收/ACK 证据。
- 2026-09-02 CANopen Node-ID 0x01 对象 0x2008:00 完成 Upload、同值 Download 和回读，真实节点端到端响应通过。
- 2026-09-02 周期调度 host/Fake-CAN 验证通过：绝对截止时间、丢期跳过、显式停止、四任务上限、第五任务拒绝、stdio 断连清理、USB 独占和 `periodic=true` 默认拒绝门控均覆盖。
- 2026-09-02 物理周期 HIL：Node 0x01 对象 0x2008:00 同值 Download，50 ms/1200 帧和 50 ms/1500 帧共 2700 次均收到 SDO 成功响应，零丢期；最大调度抖动分别为 2.909 ms、2.881 ms，测试后 profile 恢复 `periodic=false`。
- 2026-09-02 `jcan_capture` 真机 silent 验证：5.001 秒内总线无自然帧，工具按时退出并完成 CANStop/USB close，适配器在线且八项 profile 配置不变。Bus-Off 故障注入因缺少主动故障设备或分析仪延期。
- 2026-09-04 MCP 权限边界调整：单帧、周期、抓包和 CANopen 路径不再执行工程 profile 白名单；统一保留标准/扩展 ID、Classic CAN/CAN FD DLC、RTR/FD/BRS 组合等协议合法性仲裁。完整主机与 stdio 测试通过（11 项通过，5 项 HIL 未授权跳过）。
- 2026-09-04 全部 5 项 HIL 完成：内部回环/基准、5 秒 silent 抓包、可逆配置/重启、单帧物理发送和周期 CANopen 均通过。周期测试在修复跨会话接收积压后完成 50 ms/1200 帧与 50 ms/1500 帧，合计 2700 帧、零漏期，最大抖动分别为 2.747 ms 和 2.432 ms；每轮前后 0x2008:00 均为 200，配置保持基线。
