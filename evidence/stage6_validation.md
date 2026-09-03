# JCAN 阶段 6 Codex 集成验证

日期：2026-09-03

源码：`23878386f9eeefbf494c27ae9b1a081bf19eb5f1`

## MCP 注册

本机 `codex-cli 0.152.1` 使用以下 stdio 配置注册成功：

```text
name: jcan
command: /home/gtc/Desktop/workspace/JCAN/.venv/bin/python
args: /home/gtc/Desktop/workspace/JCAN/jcan_mcp.py
status: enabled
```

`codex mcp list` 和 `codex mcp get jcan` 均确认配置生效。配置位于当前用户的 Codex
配置中，仓库没有增加第二套启动脚本或重复 MCP 配置。

## Agent 端到端尝试

临时 Codex Agent 正确加载 `$jcan`，读取阶段 6 预检，并通过注册的 `jcan` MCP 调用
`jcan_scan`。结果为 `ok=true`、无 warning，但设备列表为空；目标序列号
`207F346D5650` 当前未连接。扫描证据：
`evidence/runtime/20260903T074942.335908Z-1788421782335929352-scan.json`。

Agent 按 Skill 规则停止，没有调用 `jcan_get_config` 或 `jcan_loopback_test`，没有重试，
也没有调用物理发送、抓包、SDO、周期、配置、重启、Bus-Off 或 IntoBoot 操作。首次 CLI
命令因同时使用互斥的 `--approve-for-me`/`--sandbox` 参数在 Agent 启动前被拒绝；修正为
单独使用 `--approve-for-me` 后，记录的硬件尝试次数为 1。

## Host 回归

完整回归 16 项：11 通过、5 项硬件门控跳过、0 失败，耗时 1.193 秒。MCP schema
及已有生命周期、profile、SDO 和周期调度契约保持稳定。

## 重新连接后的 Agent HIL

操作者重新连接适配器后创建新的单次预检。Codex Agent 再次正确选择 `$jcan`，并严格执行：

1. `jcan_scan`：发现唯一适配器 `207F346D5650`，USB bus 1/address 94。
2. `jcan_get_config`：读取十项原始配置基线。
3. `jcan_loopback_test`：仅调用一次，返回标准经典 CAN、扩展经典 CAN 和标准 CAN FD+BRS
   三帧，ID 分别为 `0x123`、`0x18FF0011`、`0x321`。
4. `jcan_get_config`：回环后再次读取，十项配置与基线逐字节一致。

四项调用均为 `ok=true`、warning 为空。原始证据：

- `evidence/runtime/20260903T075742.013953Z-1788422262013976075-scan.json`
- `evidence/runtime/20260903T075755.015159Z-1788422275015216334-get_config.json`
- `evidence/runtime/20260903T075811.876927Z-1788422291876947667-loopback_test.json`
- `evidence/runtime/20260903T075822.986373Z-1788422302986399872-get_config.json`

没有重试或调用抓包、物理发送、SDO、周期、配置写入、重启、Bus-Off 或 IntoBoot。

## 当前结论

阶段 6 已完成：MCP 注册、`list/get` 检查、Agent Skill 路由、缺失设备安全停止、重新连接后
的扫描/配置读取/内部回环/配置复核以及 Host 回归均通过。物理周期授权仍为
`periodic=false`。工具 schema 与 HIL 已达到后续插件封装门槛；插件本身不属于本阶段，
未自动创建。Bus-Off 故障注入继续延期。
