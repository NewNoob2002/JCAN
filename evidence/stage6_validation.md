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

## 当前结论

阶段 6 的 MCP 注册、Codex Agent 路由和缺失设备停止边界已通过。完整的
“扫描 → 配置读取 → 内部回环”HIL 尚未完成；重新连接序列号 `207F346D5650` 后，
应创建新的或确认仍适用的预检，并从一次新的 `jcan_scan` 开始，不沿用本次空扫描结果。
物理周期授权仍为 `periodic=false`，插件封装门槛尚未达到。
