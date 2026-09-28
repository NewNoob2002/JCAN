# USB 收包与 scan 排查（2026-09-18）

## 结论

- scan 的直接原因已确认并修复：宿主机缺少 99-jcan.rules，设备节点
  /dev/bus/usb/001/004 为 root:root、0664，没有用户 ACL。gtc 虽已在
  plugdev 组内，仍不能以读写方式打开设备。旧代码吞掉错误，返回空 serial。
- 经用户在系统认证窗口授权，安装仓库现有规则，reload 后仅对
  /sys/bus/usb/devices/1-2.2 触发 change。没有 USB reset 或重新插拔。
  普通用户运行新 CLI 已恢复 serial=207F346D5650，config-get 成功。
- 2026-09-15 的异常包仍不应放行。厂商 DLL 同样要求实收长度等于声明长度加
  6 字节包头；目前没有证据支持截断或忽略多出的 576 字节。
- 尚不能定位长度错误发生在固件封包、USB 传输或主机收包哪个环节，不能据此
  判定电机或 CAN 总线故障，也不能宣布 V4 长稳阻塞已解除。

## 原始包分析

来源：Linux_PROJ/robot_control/docs/verification/evidence/
p6_zero_motion_soak_v4_20260915/jcan_ack_keeper_once/jcan_session.jsonl
的 disconnected.error 十六进制数据。

逐字节提取副本：tests/data/receive_20260915.bin，3654 字节，SHA256：
53aeaae6a74ac60e98f875cacd827c01fdde8653f6c1f738b3f485ef05355414。

- 包头 4A 05 01 FF 00 0C，声明 3072 字节，实际 payload 3648 字节。
- 整包仅偏移 0 存在 4A 05 01 FF；声明边界偏移 3078 处没有新外层包头。
  不能解释成两个完整、有包头的协议包简单拼接。
- 没有 FF AA 数据帧头。FF AE 出现 404 次，其中 403 条是完整的
  FF AE 00 00 00 00 00 00 D6，CRC8(poly=0x07, init=0) 为 D6。
- payload 开头 6 字节为 00 00 00 00 00 D6，与上述记录尾部相同；是否来自
  上一次读取的剩余记录，缺少前一次原始 USB 数据，不能确认。
- 全包零起始偏移 633..647 是不连续片段：
  FF AE 00 00 00 00 00 AE 00 00 00 00 00 00 D6。
  偏移 648 恢复完整重复记录；不能仅用外层长度写错解释内部全部现象。
- 多出的 576 字节恰好是 64 条完整重复记录。这个整倍数关系不是截断依据。

## 厂商实现交叉核对（本仓库 x64/jtool.dll）

DLL SHA256：583fd346dbae7ae0b6654e3281b65bce661583306bbb3b27ebc73609b3b7f8ff。
使用 objdump -d -Mintel x64/jtool.dll 查看接收路径：

- VA 0x180007249..0x180007254：读取 16 位声明长度，加 6，与传输实收长度
  比较，不相等则跳过该包。Rust 的严格长度检查与此一致。
- VA 0x180006e96..0x180006ec6：内部记录允许 FF AA..FF B0。
  VA 0x1800070ec 的跳转表中 AE 对应 0x180006ef7，将记录长度设为 9。
- VA 0x180006fc0..0x18000700f：CRC8 校验；VA 0x180007015..0x18000701c
  仅让 AA 类型进入 CAN 数据回调。AE 的六个内容字节具体含义未确认，不能将
  这些记录称为电机故障码、有效 CAN 帧或总线错误证据。

## 本次软件修改与验证

- scan、open 共用序列号读取和错误上下文；权限失败包含总线地址和 udev 提示，
  不再把失败吞成空 serial。scan 无法读取某个匹配设备身份时返回失败。
- receive 提取纯函数，错误中记录声明长度、实际长度、差值及完整原始字节；
  接收规则未放宽，未加入丢包后自动继续逻辑。
- 用原始包做回归测试，同时覆盖正常包、短包、错误头和多余字节。
- cargo test --offline：退出 0，7 项测试通过。
- cargo build --release --offline、cargo fmt --check：退出 0。
- 实机只读命令（以下 stderr 均为空、退出码均为 0）：

    target/release/jcan --json self-test
    {"ok":true,"operation":"self-test","data":{"passed":true},"warnings":[]}

    target/release/jcan --json scan
    {"ok":true,"operation":"scan","data":[{"serial":"207F346D5650","bus":1,"address":4,"vid":"ffff","pid":"0004"}],"warnings":[]}

    target/release/jcan --json --serial 207F346D5650 config-get
    {"ok":true,"operation":"config-get","data":{"can_speed":"0C","can_customval":"00 00 01 00 04 00 59 00 1E 00","fd_speed":"06","fd_customval":"00 00 03 00 04 00 1D 00 0A 00","standard":"00","term_res":"00","busoff_recovery":"00","auto_retrans":"00","hardware_version":"00 00 00 00 00 00 00 00 00 00","id":"00 00"},"warnings":[]}

未启动 CAN、未发送数据帧、未进行长稳或实机异常包复现；不存在物理发送验证。

## 后续定位所缺的证据

需要同一次有界采集中的 USB 原始传输记录（包含异常前后的 endpoint 0x83
提交/完成长度、状态及完整字节），与 CLI 错误逐字节对照；当前日志仅保留一个
失败读取，无法追溯片段来源。先取得明确设备和模式授权，再进行采集；需要时
与厂商确认 FF AE 记录语义及固件版本行为。离线测试通过不替代这一步。
