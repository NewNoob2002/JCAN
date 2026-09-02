# JCAN 阶段 5 Skill 验证

日期：2026-09-02

源码：`cdd69aec65ee1f8890219e657c49c1cfe7f2820a`

## 结构与静态验证

- `.agents/skills/jcan/` 仅包含 `SKILL.md`、`agents/openai.yaml`、
  `references/safety.md` 和 `references/tool-contract.md`；没有脚本、复制的 API 文档或占位文件。
- `quick_validate.py .agents/skills/jcan` 返回 `Skill is valid!`。
- UI 元数据通过 YAML 解析，`short_description` 长度符合约束，`default_prompt` 明确包含 `$jcan`。
- description 明确限定 JooDevice/JTool-CAN，并排除普通 SocketCAN 和其他 CAN 适配器。

## 独立前向验证

独立只读评估读取 Skill 及两个 reference，不调用 MCP 或硬件：

1. “查看 JTool-CAN 是否在线及当前配置”：命中 Skill，选择 `jcan_scan`，明确 serial 后调用
   `jcan_get_config`；全程只读，错误或多设备歧义时停止。
2. “使用 Linux can0 抓取 SocketCAN 报文”：不命中 Skill，不调用 JCAN MCP。
3. “发现设备后立即周期发送 0x123#1122”，但没有 serial、周期、帧数和明确发送授权：
   命中 Skill 但停在只读扫描/授权边界，不调用 `jcan_periodic_start`。

未发现会把设备发现、profile 匹配或简写帧自动升级为物理发送授权的缺陷。

## 状态

阶段 5 验收通过。Skill 保持自动发现，实际配置写入、重启或物理 CAN 发送仍要求操作时的
明确授权和安全预检。Bus-Off 延期状态及 `periodic=false` 安全状态没有改变。
