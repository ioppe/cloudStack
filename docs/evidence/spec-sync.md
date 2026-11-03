# 交付文档一致性核对

## 1.0历史核对

首次交付没有既有 spec／ADR／changelog／i18n 目录。已对 README、IMPLEMENTATION、AGENT、DEPLOY、HOST-150、配置示例及当前控制端／执行端接口做一次文档核对；记录由主会话结合只读 spec-sync 报告整理。

已同步的必需项：

- CLI RPC／SSH／VNC 通过 SocketHost 访问常驻 root-only Unix agent，服务离线不直接执行或转向其他主机。
- 协议 operation `config` 的内部方法名为 `Host.configure`，避免与配置字典冲突。
- 执行端示例默认 `allow_writes:false`；.150 部署必须显式开启，默认磁盘目录为 `/var/lib/libvirt/images/vms`。
- .30 实际磁盘目录为 `/home/vm-disks`，原有 14 个 UUID 通过 `read_only_uuids` 保护；保守安装可全局只读。
- RPC 返回 `ok:false` 的业务错误仍是 exit 0；空／超长输入与 SSH 白名单拒绝的退出码分别说明。
- API 文档补齐 405／429、本地登记仅首次初始化、local 编辑身份不可变及固定 DHCP 兼容字段。
- 仅精简安装 agent 时不包含 tests，测试命令指向完整源码目录。
- 模板实际经 qemu-img convert 生成独立盘，删除与回退说明不要求改写模板。
- 授权示例包含来源 IP 限制与 forced command；初始化凭据及后续口令轮换独立说明。
- 正式管理与验收不使用 `?demo=1`，演示模式仅用于界面预览。

最终实测与证据索引见 `docs/ACCEPTANCE.md`。本次核对未引入额外产品范围。

## 1.1增量核对

已根据一次只读spec-sync核对更新：静态IPv4字段与校验、DHCP向后兼容、两类网络独立默认源、node150物理br0与原NAT共存、管理员改密及全部会话失效、本地凭据文件不自动更新、网络与软件独立回退、测试控件ID及更新记录。

HOST-150与ACCEPTANCE的首版记录已添加历史标记；v1.1迁移前快照中的default_bridge=virbr0对应变更前配置，最终实时值与核心文件哈希以 `v1.1-deployment.json` 为准。该记录确认两机真实br0存在，node150 default_bridge=br0且仍默认NAT。最终83项单元测试的第二轮日志绑定修复后的源码哈希。

当前实测结论见 `docs/ACCEPTANCE-1.1.md`；Git源码同步排除凭据、私钥、数据库、缓存和发布归档，不扩展到其他宿主机或既有VM。

## 1.1.1增量核对

依据本轮一次只读 spec-sync 核对，沿用本仓库 README／领域说明／接口文档／CHANGELOG／增量验收的同步路径；仓库无独立 spec 矩阵、ADR、schema 或 i18n 目录，本轮无新增架构边界，不强行创建对应产物。

- `networks` 增加实际网桥有效 `static_network_defaults` profile，agent 配置表与示例同步新增该可选键（缺省 `{}`）。profile CIDR 为规范网络地址，前缀 1–30，DNS 非空。
- `create` 增加 `ci_isolated`（默认 false）。静态联网缺项仅从目标网桥且 guest/profile CIDR、网关/guest CIDR 均匹配的 profile 补齐，补入网关也校验不得为 guest 自身、网络或广播地址；仍缺项写盘前拒绝，DHCP 兼容。
- 前端自动填充不覆盖手填，切换只清过期自动值，提交不偷偷补已清空字段；显式仅同网段模式无路由／DNS，提示不能访问其他网段或互联网。
- node150 的 br0 显式 profile 为 192.168.0.0/24、网关 192.168.0.1、DNS 192.168.0.1 与 8.8.8.8；node30 不推断 Tailscale DNS。新增默认值不重写已有 guest，现有 192.168.0.160 VM 保持只读。
- `?demo=1` 现在明确用于隔离的表单回归夹具，不产生管理写入；生产连通性证据来自 `e2e_static_internet.py` 的 SSH／私有 RPC 真实 guest 路径。1.0 段保留当时历史口径。
- 版本统一为 1.1.1，最终三运行时 97 项单元的证据指向 `static-internet-unit-summary.json`；实机部署、外网 HTTPS、表单与旧环境保护的最终状态见 `docs/ACCEPTANCE-1.1.1.md` 及其证据索引。

本轮修正了配置表遗漏与 demo 夹具说明；历史 1.0／1.1 验收和节点快照保留。
