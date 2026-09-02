# JCAN MCP 阶段 1–2 验证记录

日期：2026-09-02  
环境：Linux x86_64，Python 3.12.3，MCP Python SDK 1.29.1  
适配器：JTool-CAN `ffff:0004`，序列号 `207F346D5650`

## 依赖固化

- `.python-version` 固定 Python 3.12。
- `pyproject.toml` 约束 `mcp>=1.12.4,<2`。
- `uv.lock` 锁定 MCP 1.29.1 和全部运行时传递依赖。
- `uv lock --check`：通过。

## MCP stdio contract

真实 stdio 子进程完成 initialize、tools/list 和 tools/call：

```text
jcan_scan
jcan_get_config
jcan_self_test
jcan_loopback_test
jcan_loopback_benchmark
```

- `jcan_self_test` 返回结构化 `ok=true`。
- 空序列号的 `jcan_get_config` 返回结构化 `ok=false`，服务保持在线。
- stdin session 结束后 lifespan 清理单线程 USB executor。

受限命令沙箱会阻止 AnyIO 在线程中读取 stdin；同一测试在实际非沙箱 stdio 进程环境通过。Codex 注册 MCP 后仍需再做一次客户端端到端冒烟。

## MCP HIL

Agent 客户端通过 MCP 调用，而非直接调用 `jcan.py`：

- USB 扫描并匹配序列号：通过。
- 十项配置只读：通过。
- 三帧 internal silent-loopback：通过。
- 10 帧 internal silent-loopback benchmark：通过。
- MCP session 退出清理：通过。

## 未开放

- 配置写入、重启、物理总线发送、周期发送、抓包和 `IntoBoot` 均未注册为 MCP 工具。
