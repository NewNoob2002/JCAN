# JCAN 阶段 4 真机 silent 抓包验证

日期：2026-09-02

源码：`8f852b688e703e9136f9b4fd9a733229bbe697bc`

适配器：JTool-CAN `ffff:0004`，序列号 `207F346D5650`，500 kbit/s 经典 CAN。

通过 MCP `jcan_capture` 执行一次 `CAN_MODE_SILENT` 被动抓包，限制为 5000 ms 和
1000 帧，不发送 CAN/SDO 帧、不启动周期任务、不修改配置。实际持续 5001.267 ms，
收到 0 帧；当前连接的 CANopen 总线在该时间窗内没有自然流量。

测试通过的判据是物理工具入口、profile/config 门控、silent 模式、有界退出和清理。
测试后适配器仍在线，八项 profile 配置逐字节不变，CANStop 和 USB close 已完成，
所有物理帧保持 `periodic=false`。零帧结果不单独证明接收灵敏度；真实 RX 能力继续由
此前 2700 次 CANopen SDO 成功响应覆盖。
