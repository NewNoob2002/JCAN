# JCAN MCP 阶段 1–3 真机 HIL 验证

日期：2026-09-02

源码：`88cc8a6c29385df761db2dce98798f529dc05c77`

环境：Linux 7.0.0-30-generic x86_64，Python 3.12.3，MCP SDK 1.29.1

适配器：JTool-CAN `ffff:0004`，序列号 `207F346D5650`

## 安全边界

- 内部 `CAN_MODE_SILENT_LOOPBACK`，不驱动物理 CAN/CAN-FD 总线。
- 配置写入前读取十项基线，失败时恢复；未调用 `IntoBoot`。
- 仅执行一次普通重启，要求先观察离线，再按相同序列号重新上线。
- 抓包、物理发送和周期发送工具仍未注册。

## 结果

| 项目 | 结果 | 证据 |
|---|---|---|
| USB 扫描 | 通过，测试前 `001:008` | `evidence/runtime/*-scan.json` |
| 十项配置读取 | 通过 | `evidence/runtime/*-get_config.json` |
| 三帧内部回环 | 标准 CAN、扩展 CAN、CAN FD+BRS 全部通过 | `evidence/runtime/*-loopback_test.json` |
| 10 帧回环基准 | 2132.937 fps；延迟 min/avg/P95/max = 0.414/0.456/0.524/0.524 ms | `evidence/runtime/*-loopback_benchmark.json` |
| 具名配置应用 | 使用当前四项开关值写入并回读，`changed_fields=[]` | `evidence/runtime/*-apply_config.json` |
| 配置往返 | 全部可恢复字段写入、回读并恢复，`restored=true` | `evidence/runtime/*-config_roundtrip_test.json` |
| 普通重启 | 观察离线后以相同序列号重新枚举到 `001:009` | `evidence/runtime/*-reboot.json` |
| 测试后基线 | 十项配置逐字节等于测试前 | 后置 `config-dump` |

执行命令：

```text
JCAN_TEST_SERIAL=207F346D5650 JCAN_STAGE3_TEST_SERIAL=207F346D5650 \
JCAN_EVIDENCE_DIR=evidence/runtime .venv/bin/python -m unittest -v test_jcan_mcp.py
```

结果：`Ran 6 tests in 4.419s`，`OK`，首次运行，无失败、跳过或重试。

测试后配置：

```text
can_speed: 0c
can_customval: 00 00 01 00 04 00 59 00 1e 00
fd_speed: 06
fd_customval: 00 00 03 00 04 00 1d 00 0a 00
standard: 00
term_res: 00
busoff_recovery: 00
auto_retrans: 00
hardware_version: 00 00 00 00 00 00 00 00 00 00
id: 00 00
```
