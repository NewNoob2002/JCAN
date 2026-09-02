# JCAN 阶段 4 物理总线 profile 主机验证

日期：2026-09-02

源码：`52676fdcd78e0d813627fdcc193720b2fe33f0f1`

## 已实现

- `jcan_bus_profile.toml` 使用 Python 3.12 标准库 TOML，无新增依赖。
- profile 严格校验 schema、批准状态、适配器序列号、声明 bitrate、FD 标准、抓包上限、周期边界和清理状态。
- 物理操作前逐字节核对仲裁/数据相位配置、FD 标准、终端电阻、Bus-Off 恢复和自动重传。
- TX 白名单核对 CAN ID、标准/扩展、CAN/CAN-FD、BRS、remote、DLC 和精确 payload。
- `jcan_capture` 使用 silent 模式，并同时受 `duration_ms` 和 `max_frames` 限制。
- `jcan_send_once` 每次只发送一帧，结束时执行 CANStop。
- `jcan_bus_profile_status` 只读返回当前 profile 状态。

## 默认安全状态

仓库 profile 当前为 `enabled=false`、`approved=false`、bitrate 为 0、帧规则为空。
抓包和发送请求会在创建 libusb context 或 USB handle 前被拒绝。

## 验证结果

- 允许的单帧发送：Fake CAN 通过。
- 非白名单 payload：在 USB 打开前拒绝。
- 设备原始配置不匹配：在 CANStart 前拒绝。
- 未批准/禁用 profile：在 USB 打开前拒绝。
- RX ID/DLC/payload 过滤和有界 silent 抓包：Fake CAN 通过。
- MCP tools/list schema 和错误后服务存活：通过。
- 全部回归：`Ran 8 tests in 0.468s`，6 通过，2 项既有 HIL 门控跳过。

## 尚未完成

- 未提供真实 nominal/data bitrate、FD 标准与 preset/raw 配置对应关系。
- 未提供物理节点、线束/终端状态以及允许的 TX/RX ID、帧标志、DLC 和 TX payload。
- 周期任务调度与物理总线 HIL 尚未实施。
