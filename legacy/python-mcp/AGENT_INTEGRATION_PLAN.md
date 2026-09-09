# JCAN Agent Skill + MCP 实施计划

> 历史方案。2026-09-09 起由 Rust CLI 1.0 和仓库根目录 AGENTS.md 取代，不再作为 Agent 的执行入口。

日期：2026-09-02  
状态：阶段 1–6 已完成；Bus-Off 故障注入延期，物理周期授权保持关闭

## 目标与结论

为 Codex/Agent 提供安全、稳定、可发现的 JCAN 操作能力：

- 使用一个简短的 JCAN Skill 指导工具选择、验证顺序和安全边界。
- 使用 Python stdio MCP Server 复用已通过真机验证的 `jcan.py`。
- MCP 只承载低频控制命令；收包、周期发送和 USB 生命周期留在服务内部。
- 第一版不使用 Rust、不做 Python/Rust 混合、不封装插件；工具契约稳定后再考虑分发。

## 非目标

- 不开发桌面 GUI。
- 不支持非 JTool-CAN/SocketCAN 设备。
- 不执行 `JCANIntoBoot`。
- 未建立物理总线配置文件前，不开放真实 CAN 总线发送和周期发送。
- 不让 Agent 通过 MCP 逐帧拉取高速数据。

## 最小结构

```text
Codex Agent
  ├─ JCAN Skill：何时调用、先读后写、安全约束
  └─ stdio MCP tools
       ↓
Python MCP Server
  ├─ lifespan：初始化/清理 DeviceManager
  ├─ 单工作线程：独占 libusb context 和 USB handle
  ├─ 周期调度器：monotonic 绝对截止时间
  └─ jcan.py：协议、配置、帧解析和设备操作
```

MCP Server 退出时必须停止 CAN、取消周期任务并关闭 USB handle。stdout 只写 MCP 协议，诊断日志写 stderr 或证据文件。

## 计划阶段

### 1. 固化项目与依赖（完成）

交付：

- `pyproject.toml` 和 `uv.lock`，Python 3.12。
- 仅新增官方 MCP Python SDK；继续通过 `ctypes` 使用系统 libusb，不再增加 USB 依赖。
- MCP 入口点 `jcan-mcp`，可由 `uv run` 启动。

验收：

- 全新环境可按锁文件安装。
- `jcan.py self-test` 和语法检查保持通过。

### 2. 建立只读 MCP 骨架（完成）

首批工具：

| 工具 | 行为 | 风险 |
|---|---|---|
| `jcan_scan` | 枚举设备与序列号 | 只读 |
| `jcan_get_config` | 读取十项配置 | 只读 |
| `jcan_self_test` | 运行纯主机协议测试 | 无硬件 |
| `jcan_loopback_test` | 内部 silent-loopback | 有界设备操作 |
| `jcan_loopback_benchmark` | 有界内部回环基准 | 有界设备操作 |

实现约束：

- 所有硬件工具接受明确 `serial`，不默认选择第一台设备。
- 返回统一结构：`ok`、`serial`、`operation`、`data`、`warnings`、`evidence_path`。
- Agent 获得摘要和证据路径，不返回无限帧流或大块日志。

验收：

- MCP `tools/list` 和每个工具的输入 schema 稳定。
- 服务从 stdin EOF 退出时完成清理。
- 无设备、权限不足、USB 断开均返回结构化错误，不崩溃。

### 3. 增加可恢复配置操作（完成）

工具：

- `jcan_apply_config`：仅接受具名字段，不接受任意配置键。
- `jcan_config_roundtrip_test`：调用现有备份、写入、回读、恢复流程。
- `jcan_reboot`：单独工具，要求明确序列号并在重启后验证重新枚举。

安全要求：

- 每次配置修改执行“读取基线 → 校验参数 → 写入 → 回读”。
- 失败时恢复基线，并在结果中报告恢复是否成功。
- `hardware_version`、`device_id`、重启不能与普通配置批量混在一次调用中。
- 第一版不注册 `into_boot` 工具。

验收：

- Fake USB 测试覆盖成功、拒绝、超时、短回复和恢复失败。
- 真机配置往返后与操作前逐字节一致。

### 4. 增加抓包和周期任务（功能闭环，Bus-Off 延期）

已实现默认拒绝的 `jcan_bus_profile.toml`。只有 `enabled=true`、`approved=true`、
序列号/设备原始配置匹配且帧命中白名单后，物理工具才可访问 USB。当前仓库
profile 已按操作者提供的规格启用：500 kbit/s、经典 CAN 标准帧 TX、ID `0x7FF`、
DLC 4、精确 payload `00 00 00 00`。真机配置匹配，且一次白名单标准帧发送请求
已通过 HIL。随后通过 CANopen Node-ID `0x01` 对 RAM 对象 `0x2008:00` 完成
SDO Upload → 同值 Download → Upload 回读，驱动器返回合法 `0x581` 响应，已证明
节点端到端接收、协议处理与双向返回；未覆盖示波器级波形、采样点或 ACK 槽观测。

已实现：

- `jcan_capture`：有界 `duration_ms`/`max_frames`，返回统计、样本和日志路径。
- `jcan_send_once`：必须匹配 profile 中允许的序列号、CAN ID、帧类型和 DLC。
- `jcan_bus_profile_status`：只读检查 profile 与安全门状态。
- `jcan_sdo_read`：仅允许 profile 明确列出的 expedited SDO Upload。
- `jcan_sdo_u16_same_value_test`：读取 U16 基线、仅写回同值并立即回读，不保存 EEPROM。
- `jcan_periodic_start`、`jcan_periodic_list`、`jcan_periodic_stop`：服务内部持久 CAN 会话调度；任务必须有明确帧数上限并由 profile 单独设置 `periodic=true`。

profile 至少包含：适配器序列号、nominal/data bitrate、CAN-FD 标准、允许 ID/方向/DLC、最小周期、最大任务数和清理状态。

周期策略：

- 使用单调时钟绝对截止时间，避免累计漂移。
- 默认丢期后跳过过期实例，不补发突发帧。
- 每个任务记录 planned/actual 时间和抖动。
- MCP 断开或进程退出时停止全部周期任务。
- 周期会话独占适配器；运行期间其他硬件工具默认拒绝，避免并发使用同一 USB handle。

验收：

- Host/Fake-CAN 已验证启停、取消、抖动统计、四任务上限、第五任务拒绝和 stdio 断连清理。
- 物理总线已验证允许帧、500 kbit/s、真实 CANopen 节点响应、周期抖动和有界 silent 抓包。
- 未匹配 profile 的发送请求在 USB 写入前拒绝。
- Bus-Off/错误状态故障注入因缺少主动故障设备或分析仪延期，不声明通过。

### 5. 创建 JCAN Skill（完成）

位置：`.agents/skills/jcan/`。

最小文件：

```text
.agents/skills/jcan/
├── SKILL.md
├── agents/openai.yaml
└── references/
    ├── safety.md
    └── tool-contract.md
```

Skill 只记录 Agent 真正需要的决策：

- 何时使用 JCAN MCP。
- 先扫描、锁定序列号，再读取基线。
- 只读、内部回环、配置写入、物理总线操作的风险分级。
- 何时需要读取两个 reference。
- 禁止把设备检测结果当成发送或维护授权。

不复制 `JCAN_API.md`，不内嵌协议实现，不增加脚本；确定性操作全部由 MCP 完成。

验收：

- `quick_validate.py` 已通过。
- Skill 描述能命中 JTool-CAN 请求而不吸引普通 SocketCAN 任务。
- 独立前向验证确认 Agent 先选择 `jcan_scan`/`jcan_get_config`，并在缺少物理发送参数或授权时停止。

### 6. Codex 集成与发布门槛（完成）

- 已使用 `codex mcp add` 注册本地 stdio Server，`codex mcp list/get` 显示 `jcan` 为 enabled stdio。
- Agent 已正确选择 `$jcan`，完成扫描 → 十项配置基线 → 一次三帧内部回环 → 十项配置复核；全程无 warning，配置逐字节不变。
- 工具 schema 与 Host 回归稳定，Agent HIL 通过；已达到后续插件封装门槛，但阶段 6 不自动增加插件。

## 测试矩阵

| 层级 | 必须验证 |
|---|---|
| Host unit | 包头、帧解析、配置编码、参数边界、周期调度算法 |
| MCP contract | 工具 schema、结构化错误、超时、取消、stdio 退出清理 |
| Fake USB | 断开、超时、短包、拒绝、配置恢复失败 |
| HIL/internal | 扫描、silent-loopback、配置往返、重启、周期任务停止 |
| Physical bus | bitrate、当前 profile 的经典 CAN、允许 ID/DLC、周期抖动；Bus-Off 延期 |
| Skill | 路由准确、安全顺序、证据输出、授权边界 |

## 复审结果

结论：**阶段 1–4 功能验收通过；Bus-Off 故障注入延期**。

通过项：

- Python 方案复用已验证实现，当前性能没有支持 Rust 重写的证据。
- Skill 与 MCP 职责分离清楚。
- stdio + lifespan 适合管理单个长生命周期硬件资源。
- 分阶段开放工具能避免一开始暴露危险或高吞吐接口。

实施前必须保持的条件：

1. MCP 是控制面，禁止逐帧 Agent 调用。
2. 所有活动操作必须指定序列号。
3. 不提供任意配置键写入。
4. 物理发送必须受 bus profile 限制。
5. 第一版不提供 `IntoBoot`。
6. 所有退出路径必须停止 CAN、周期任务并关闭 USB。

阶段 1–3 已满足以上条件。阶段 4 已完成 profile、授权审计、有界 silent 抓包、
白名单单帧发送、CANopen SDO 和有界周期调度的 host/Fake CAN 验证，并完成真机节点
端到端 SDO HIL，并完成 50 ms/60 秒/1200 帧和 50 ms/1500 帧两项真机周期测试；
2700 次 Download 均收到合法节点响应，零丢期，测试前后对象值保持 200。测试完成后
物理 profile 已恢复 `periodic=false`。补充验证覆盖四任务上限、第五任务拒绝、活动任务下
stdio 断连后的 CANStop/USB close，以及一次 5 秒真机 silent 抓包；抓包期间总线无自然帧，
工具仍按上限退出且适配器配置保持不变。Bus-Off/错误状态故障注入等待具备主动故障设备
或分析仪后单独执行；该验收不替代示波器级物理层诊断。
