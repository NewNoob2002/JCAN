# JCAN 阶段 4 周期调度 Host 验证

日期：2026-09-02

源码：`89708ecbc50885ef489e590af3db9152582c135d`

## 已验证

- MCP 注册 `jcan_periodic_start`、`jcan_periodic_list`、`jcan_periodic_stop`。
- 一个持久 CAN 会话承载最多 profile 限定数量的周期任务，不逐帧重复打开 USB。
- 使用单调时钟绝对截止时间；发送耗时超过周期时记录 `missed_periods` 并跳过过期实例，不突发补发。
- 每帧记录 planned/actual/jitter，任务完成后生成 JSON evidence。
- 任务限制为 1–1000 帧，period 为 profile 下限至 60000 ms。
- 显式停止等待在途发送结束；最后一个任务退出后执行 CANStop 和 USB close。
- 周期会话运行期间，同一适配器的其他硬件工具默认拒绝。
- TX 规则必须显式包含 `periodic=true`；单帧或 SDO 授权不会自动升级为周期授权。

## 测试结果

完整回归：12 项，9 项通过，3 项真机门控跳过，0 项失败；耗时 0.592 秒。

Fake-CAN 使用 10 ms 周期和 25 ms 模拟发送耗时，确认发生丢期计数且只发送目标 3 帧；另一路 100 帧任务在发送 2 帧后停止，停止后无新增帧，并完成 CANStop/close。

## 当前物理门状态

实际 `jcan_bus_profile.toml` 中所有 frame 的 `periodic` 均为默认 `false`。因此本次没有驱动物理总线；需要操作者明确周期帧、period 和 count 后再建立安全预检并执行物理 HIL。
