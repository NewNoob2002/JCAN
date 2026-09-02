# JCAN 阶段 4 物理总线 profile 激活记录

日期：2026-09-02

源码：`067300613ceec0ba4f2999e92e78344b90d1b509`

## 激活参数

- nominal bitrate：500000 bit/s。
- 协议：经典 CAN 2.0B；按扩展帧解释。
- 方向：TX。
- CAN ID：`0x7FF`。
- DLC：4。
- 唯一允许 payload：`00 00 00 00`。
- 适配器：`207F346D5650`。
- 审计：`enabled=true`、`approved=true`，记录授权来源和 UTC 批准时间。

## 验证

- TOML profile 严格解析：通过。
- MCP stdio 与 host 回归：8 项中 6 项通过，2 项既有 HIL 门控跳过；耗时 0.476 秒。
- 非白名单 payload `00 00 00 01`：在 USB 打开前拒绝。
- 真机八项总线相关原始配置与 profile：逐字节匹配，USB `001:009`。
- 未执行 CANStart，未发送物理帧。

## 保留风险

仓库没有厂商 preset-to-bitrate 映射表，因此 `can_speed=0x0C` 对应 500 kbit/s
由操作者提供的总线规格声明，当前只能验证设备原始值未偏离 profile。

下一步需在确认线束、公共地、终端和接收节点安全后，对唯一白名单帧执行一次有界
物理发送 HIL；周期任务仍未实现。
