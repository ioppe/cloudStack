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
