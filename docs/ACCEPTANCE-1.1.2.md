# v1.1.2 桥接 VM 地址显示修复

## 原因与已确认事实

旧实现只读取 guest-agent、libvirt DHCP 租约和宿主机邻居表。桥接 VM 的地址可能来自外部 DHCP 或 guest 静态配置，不在 libvirt 租约中；guest-agent 未连接且宿主机未缓存邻居时会返回空地址。前端把运行中空地址一律显示“获取中”，同时缺少持续 VM 列表刷新，导致地址可用仍长期不显示。

只读诊断及实际 ARP 回应确认：

| 宿主机 | VM | MAC | 实际观察地址 |
|---|---|---|---|
| 192.168.0.150 | ubutnu24.04 | 52:54:00:9c:98:ea | 192.168.0.160 |
| 192.168.0.30 | ubuntu5 | 52:54:00:3a:56:b5 | 192.168.0.28、192.168.0.211 |

`ubutnu24.04` 有 guest-agent 通道但未连接，`ubuntu5` 无该通道；两者网桥链路均 up。探测结果是对应 MAC 对 ARP 的真实响应，不是从创建参数猜出的值。本轮不调整其 guest 地址。证据：`evidence/ip-discovery-diagnosis.json`，修复前状态及配置基线：`evidence/ip-discovery-baseline.json`。

## 修复契约

- 新增显式 `bridge_ip_discovery` 配置（默认 false），当前两宿主机启用。仅为运行中桥接且缺少被动地址来源的 VM 发现地址，现有 agent／DHCP／邻居表优先级保留。
- 探测来自宿主机实际本地网桥的 IPv4 子网，仅 /24–/30、网段最多 256 个地址、每轮最多 253 个 ARP 请求，不扫描端口、不写 guest／XML／宿主机网络。单轮至多 3 秒、后台异步、每桥至少 30 秒间隔、正结果缓存 120 秒、阴性结果缓存 30 秒，使用单调时钟。仅接受当前 VM MAC 的有效真实响应，保留多地址，并剪枝过期／离开的目标及其迟到结果。
- API 兼容保留 `ip_addresses` 和网卡 `ips`，增加 `ip_source`、`ip_status` 与 `ip_status_reason`。前端区分实际地址、探测中、未探测到和未运行。
- 列表在已登录且可见时每5秒轻量刷新，不清空当前数据；切页、隐藏、主机切换或退出登录停止旧轮询，迟到结果按主机隔离。

## 最终验证结果

2026-11-04 完成部署与验收，两端执行端已启用发现，控制端 `/health` 返回 `{"ok":true,"version":"1.1.2"}`。本地、node30、node150 的7项部署文件哈希一致；最终 `ipdiscovery.py` SHA-256 为 `c49b22214acf20b8c0ec6c1a63fa220aa3dc7f86539060f27c033142bf2b0614`。部署备份、仅增补发现开关及末尾空行规范化的 AST 等价证明见 [部署记录](evidence/ip-discovery-deployment.json)；最终公开文件同步与健康核验见 [源码清单](evidence/ip-discovery-final-source.json)。

| 验证 | 结果 | 证据 |
|---|---|---|
| 本机 Python 3.13.7 单元测试 | 157/157，0失败／错误／跳过 | [本机日志](evidence/ip-discovery-unit-local.log) |
| node30 `/usr/bin/python3` 3.6.8 单元测试 | 157/157，0失败／错误／跳过 | [node30日志](evidence/ip-discovery-unit-node30.log) |
| node150 Python 3.12.3 单元测试 | 157/157，0失败／错误／跳过 | [node150日志](evidence/ip-discovery-unit-node150.log) |
| 真实 SSH／私有 RPC／ARP／临时 VM 生命周期 | 11/11，exit 0 | [实机报告](evidence/ip-discovery-e2e.json)、[日志](evidence/ip-discovery-e2e.log) |
| 显式 demo 浏览器夹具 | 8/8 | [界面报告](evidence/ip-discovery-ui.json)、[日志](evidence/ip-discovery-ui.log) |

单元快照 `abc2b40c5712bc0a6fe4c8e9e28b0e6b192af9b980117fcf5d07f70e44693a55` 包含最终运行代码及全部单元用例，详细哈希与语法检查见 [单元汇总](evidence/ip-discovery-unit-summary.json)。之后只修正实机验收脚本的停机状态断言：操作接口返回“已关机”，列表保留原始 `shut off`，最终同时检查两者及 `not_running`；运行源码和单元用例未变。最终实机脚本 SHA-256 为 `bb3aa4b3b69e76a409702bd23c95f3276d95489c44d859c17d8c7fb46d6f7570`，脚本与嵌入 Python 编译通过并完整执行。

实机验收确认现有 `.150` VM 地址 `.160` 可见；直接 ARP 在 0.836 秒内取得 `.160`，在 `.30` 的 0.814 秒查询中保留同一 MAC 的 `.28` 和 `.211`。没有登录既有 guest、读取其磁盘／seed，或修改其地址。

专用 `platform-ip-discovery-accept` 使用冲突探测为空的 `.248`，新建后不登录、不 ping 即经 `ip_source:arp` 出现地址；停机后 `ip_status:not_running`，重新启动及 vnet 接口重编号后仍自动发现 `.248`。最终删除专用域及 qcow2／seed 两文件，`removed_count:2`、`skipped_count:0`，无残留。

前后逐台比对 `.30` 14台、`.150` 1台既有 VM 的状态与 inactive XML 哈希一致；网桥、宿主机 IPv4／默认路由／DNS、私有配置、存储目录和 `home.mount` 依赖均保留。两端有效只读名单仍为空，agent socket 为 root 0600，相关服务 active/enabled，旧8088返回200且其三个源／配置／数据库文件哈希不变。隔离单元测试目录也已清理。

浏览器8项验证覆盖状态变化与自动刷新、无响应提示、停机旧地址隐藏、切主机迟到响应隔离、切页／页面隐藏／退出暂停轮询；使用明确的 demo 夹具，不作为生产登录或 guest 联网证据。初始单元夹具失败与两轮实机脚本适配失败原样保存在 `ip-discovery-unit-*-round1.*`、`ip-discovery-e2e-round1.*`、`ip-discovery-e2e-round2.*`；最终通过结果使用默认文件名，历史失败没有重标为成功。

## 保留与限制

本轮不读取既有 guest 的业务数据或 seed，不写其网络／磁盘，不安装 guest-agent。所有既有 VM 的当前状态和 inactive XML 按本轮基线保护；用户后续明确授权解除的原14台只读名单保持为空，不回退到首版权限配置。旧8088、物理网桥、宿主机地址／路由／DNS、存储及挂载依赖保留。

guest 不响应 ARP、未配置本地小网段地址，或只有非本地／IPv6地址时，可能仍显示“未探测到”，应通过控制台或 guest-agent确认；平台无法从空结果断言 guest 没有网络。地址发现也不代表外部联网或业务健康检查通过。前一轮静态网络修复和现有 `.160` guest 的路由／DNS说明继续见 `ACCEPTANCE-1.1.1.md`。

原始测试日志保留 Python 版本横幅和断言差异中的尾空格，`.gitattributes` 仅对 `docs/evidence/ip-discovery-*.log` 放宽 `blank-at-eol`；源代码和其他交付文件维持格式检查，日志哈希不因格式整理改变。
