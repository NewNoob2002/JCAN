# JCAN 阶段 4 物理单帧发送 HIL

日期：2026-09-02

源码：`ce7469c20f57ff99aabf712471f45125059c96ba`

适配器：JTool-CAN `ffff:0004`，序列号 `207F346D5650`，USB `001:009`。

## 刺激

- nominal bitrate：500000 bit/s（操作者声明）。
- 经典 CAN 标准数据帧。
- ID：`0x7FF`。
- DLC：4。
- payload：`00 00 00 00`。
- 次数：一次，无应用层重试、无周期任务；profile 中自动重传为关闭。

## 结果

- 发送前序列号、profile 批准状态和八项设备配置匹配：通过。
- MCP `jcan_send_once`：`ok=true`，命中规则 `tx-7ff-zero4`，无警告。
- 测试：`Ran 1 test in 0.414s`，首次运行通过。
- 发送后适配器仍在线于 `001:009`，八项设备配置仍匹配 profile。
- CANStop 由 `jcan_send_once` 的 `finally` 路径执行。

## 证据边界

该结果证明 MCP profile 门控、USB 命令和设备发送请求被接受。由于未连接独立接收
节点、示波器或 CAN 分析仪，本次不能证明帧已在物理导线上被观测、ACK 或接收。
