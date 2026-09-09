# JCAN 后续验证：已复现的问题

以下为修复前发现及复现记录。用户随后授权修复，三项问题均已修复；见文末修复结果。全部模拟均使用 FakePhysicalCan，不访问 USB。

## 真机验证

- 适配器 207F346D5650：3 秒被动抓包接收到 0x581 和 0x701，总计 9 帧。历史缓存与实时流量未区分，timestamp 全为 0，不能用这些样本证明实时周期。
- 一次 SDO upload 读取节点 1 / 0x1017:00 成功：E8 03，即 1000。用户确认该设备单位为 0.5 ms，对应 500 ms。此前 RK3588 实测平均 499.061 ms，与之接近；撤销单位异常结论。
- 读取前后适配器完整配置一致，无活动周期任务、无会话清理错误。

## 1. SDO 同批响应丢失（已在主机复现）

位置：jcan.py:419 的 wait_frame，及 sdo_read / sdo_write。

触发：一次 USB read 同时包含两帧 0x581，第一帧属于另一对象，第二帧才是请求对象。CanStreamParser.feed 一次解析并消费整批帧；wait_frame 在第一条 ID 匹配时提前返回，剩余帧被丢弃。SDO 层拒绝第一条对象不符的响应后，再等待时已找不到正确响应。

复现结果：upload 和 download 都在正确响应已经进入 USB 数据的情况下报告超时。不是线缆或对端未响应。

建议：将完整 SDO 响应条件放进同一批次的帧遍历，或在共享等待层保留未处理帧；同时覆盖上传/下载，避免只修一条调用路径。

## 2. 周期清理期间设备锁提前释放（已在主机复现）

位置：jcan_mcp.py:495 的 PeriodicManager._run，jcan_mcp.py:1073 的 _hardware_call。

触发：最后一个周期任务结束，active 清空，退出 hardware_gate 的 async with，然后才在外层 finally 中执行 drain/stop/close。is_active 只检查 active。此时普通硬件调用可以通过两次 is_active 检查并取得锁，在旧 USB 会话仍未停止/关闭时执行。

复现结果：刻意暂停清理后，设备锁未持有、active=false，但 stopped=false、closed=false；第二个 _hardware_call 成功执行并观察到旧 USB 会话仍打开。串行执行器只串行化单个函数，不能替代覆盖完整会话的互斥。

建议：把 CANStop/USB close 也放在 hardware_gate 保护范围内，直到清理完成才允许新硬件操作。

## 3. 周期启动失败未执行 CANStop（已在主机复现）

位置：jcan_mcp.py:469 的 PeriodicManager._open。

触发：CANStart 成功，随后初始 _drain_receive 失败。except 只调用 close；_open 未返回对象，外层 _run 的 can 仍为 None，外层 finally 无法补做 CANStop。

复现结果：模拟 drain 故障后 started=true、closed=true、CANStop_called=false。尚未对真实适配器注入此故障，不能判断设备自行停止与否。

建议：启动尝试后的异常路径也执行 CANStop，并在其失败时仍关闭 USB、保留原始故障与清理错误。

## 仍待物理验证

单帧默认 settle_ms 仍为 0；这次验证没有修复先前过早停止问题。周期尾帧也没有物理发送完成确认，不能把 sent_count 直接当成 candump 实收帧数。未追加周期发送、对象写入、配置修改或重启。

## 可重复执行

从仓库根目录运行：

    .venv/bin/python evidence/reproduce_followup_issues.py

输出：20260907_followup_host_reproduction.json。该 JSON 保留修复前的失败证据；脚本现已转为运行正式回归测试，验证修复后的正常行为。

真机证据：

- runtime/20260907T054312.211577Z-1788759792211615562-capture.json
- runtime/20260907T054353.762815Z-1788759833762839715-sdo_read.json
- runtime/20260907T054741.098139Z-1788760061098169044-periodic_list.json
- runtime/20260907T054811.247579Z-1788760091247603323-get_config.json

## 修复结果

2026-09-07 用户授权后，已修复上述三项缺陷：共享 wait_frame 在返回前应用完整 SDO 匹配条件；周期 hardware_gate 覆盖启动、发送及清理全过程；启动失败先尝试 CANStop，再尝试 USB close，清理错误连同原始故障保留，会话结果标记 failed。

新增四项回归测试（含多个子场景）。修复前针对前三项测试均复现失败；修复后完整主机与 stdio 测试共 19 项通过，耗时 1.308 秒，git diff --check 通过。日志：20260907_fixes_final_tests.log；哈希和结果：20260907_fixes_validation.json。

运行中的 MCP 尚未重启加载；本轮未新增物理发送，尚未进行修复后真机测试。原始单帧的默认零等待及周期尾帧的物理完成确认不属于这三项修复，仍需独立验证。现有 CANopen 工具支持一次调用内发送并等待匹配响应，但没有通用任意值写入工具或长期后台 RX 订阅。
