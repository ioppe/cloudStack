# v1.1.1 静态 IP 联网修复与增量验收

## 问题与范围

旧创建流程允许关闭 DHCP 后仅填写固定 IP，而网关和 DNS 均可为空。guest 因此缺少默认路由与域名解析配置；IP 固定并不等于能访问其他网段或互联网。已只读确认用户现有 VM `ubutnu24.04`（UUID `5bf2a360-20d4-4c05-99de-499796069fef`，192.168.0.160/24）接在 node150 的 br0，没有对它的 guest、seed、磁盘或运行配置执行修改。

本次修复新建流程，保留原 NAT、物理网桥、宿主机管理地址、存储目录与所有已有 VM。原 `.30:8088` 和 `/opt/vm-panel` 保留。旧 v1.1 验收是历史证据；本文件记录 v1.1.1 的当前结果。

## 最终行为

- 静态联网缺省 `ci_isolated:false`，最终网关与 DNS 都必须存在；只补缺项，保留合法手填值。
- 默认值来自执行端按网桥明确配置的 `static_network_defaults`。guest IP 必须在 profile CIDR 内，profile 网关必须在 guest 自己的 CIDR 内，补入后仍校验不能为 guest 自身、网络或广播地址。匹配失败且缺项会在磁盘／seed 写入前拒绝。
- node150 的 br0 配置 `192.168.0.0/24`、网关 `192.168.0.1`、DNS `192.168.0.1` 与 `8.8.8.8`；node30 不从其 Tailscale 解析器自动推断 guest 默认 DNS。profile CIDR 必须为规范网络地址，无效配置不套用。
- 前端匹配网桥／网段时自动填充，保留手填值；切换后只清除过期自动值。清空网关或 DNS 后提交会提示补齐。
- 显式“仅同网段内网”／`ci_isolated:true` 不配置默认路由和 DNS，同时传非空网关／DNS 会被拒绝。NAT／ISO／关闭 cloud-init 保持原 DHCP 行为。

字段与使用步骤见 [NETWORKS.md](NETWORKS.md)、[AGENT.md](AGENT.md)、[IMPLEMENTATION.md](IMPLEMENTATION.md) 和 [UI.md](UI.md)。

## 最终验收：通过

| 项目 | 结果 | 证据 |
|---|---|---|
| Python 3.13.7／3.6.8／3.12.3 完整单元 | 各 97/97，0 失败、0 跳过；JS 语法与配置 JSON 通过 | `evidence/static-internet-unit-summary.json` 及三份正式日志 |
| 两节点部署与运行版本 | 新服务 active/enabled，`/health` 返回 1.1.1；node150 显式 profile 生效，其他配置字段保持不变 | `evidence/static-internet-deployment.json`、`static-internet-http-ready.json` |
| 专用临时 guest 真实联网 | 9/9，通过静态 IP、网关、DNS、MAC、外部证书校验 HTTPS 与清理 | `evidence/static-internet-e2e.json`、`static-internet-e2e.log` |
| 独立演示表单回归 | 7/7，自动值／手填保留／缺项拦截／仅内网 payload／NAT 兼容 | `evidence/static-internet-forms.json`、`static-internet-forms.log` |
| 旧环境与宿主机保护 | 9/9，15 台既有 VM 状态及 inactive XML 哈希、旧面板三文件哈希、8088、宿主机地址／路由／DNS、agent socket 与挂载依赖均保持 | `evidence/static-internet-environment.json` |

实际临时 VM `f2544333-219e-4122-865a-8240221904ae`，地址 `192.168.0.248/24`（三次 ARP 冲突探测后使用），2 vCPU／2 GiB／8 GiB，磁盘位于 `/home/vm-disks`。请求刻意省略 `ci_gateway` 和 `ci_dns`；guest ens3 的 MAC 与 libvirt 一致，实际默认路由经 `192.168.0.1`，DNS 为 `192.168.0.1`、`8.8.8.8`。

guest 将 `example.com` 解析为 `104.20.23.154`／`172.66.147.243`，实际连接 `104.20.23.154:443`，证书校验成功，返回 `HTTP/1.1 200 OK`。不匹配 profile 的 `198.18.0.248/24` 请求被拒，未创建域、磁盘或 seed。临时 VM 已删除，专用磁盘及 seed 共 2 个文件已清理，0 跳过；现有 `.160` 用户 VM 仍 running 且配置哈希未变。

首轮两项失败属于验收适配：`ip -j -4 address` 不含 MAC，现通过 `ip -j link` 与接口名关联；agent-browser 0.20.7 的 `uncheck` 未改变复选框，现用原生点击并断言实际 checked／disabled 状态。全部原业务断言保留，最终复测退出码均为 0，首轮失败证据保留为 `*-round1.*`。产品源码仍与最终三版本 97 项测试哈希一致。表单使用明确隔离的 `?demo=1` 夹具，生产联网使用 SSH／私有 RPC；管理员密码及会话未改动。

部署重启后的首次 HTTP 探测短暂连接拒绝，后续就绪检查已成功；使用旧 systemd 支持的 `is-active`／`is-enabled` 核实服务。代码与 node150 配置备份位于服务器 `/var/lib/vm-platform/backups/`，分别为 `platform-code-before-static-internet-1.1.1.tar.gz` 与 `agent-before-static-internet-1.1.1.json`。

## 已有 guest 的补正

创建逻辑修复不会重新执行已有 VM 的 cloud-init。现有 192.168.0.160 guest 仍需在自己的 Netplan 中添加默认路由与 DNS；不得通过重建 seed 或覆盖业务磁盘来修复。根据当前局域网规划，应保留原 MAC 匹配与地址，在对应网卡段补充：

```yaml
routes:
  - to: default
    via: 192.168.0.1
nameservers:
  addresses: [192.168.0.1, 8.8.8.8]
```

该段是已有 VM 的修复说明，本轮未对该 VM 执行。应用前应备份其实际配置并通过控制台使用 `sudo netplan try` 验证，再确认保留；配置文件名与网卡段以 guest 实际情况为准。
