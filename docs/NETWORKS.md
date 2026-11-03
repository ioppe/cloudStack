# NAT、桥接与虚拟机静态 IPv4

## 当前 node150 网络

v1.1 根据后续功能请求新增桥接，不删除原 NAT：

| 用途 | 接口／来源 | 配置 |
|---|---|---|
| 宿主机管理与物理桥接 | br0，端口 eno2np1 | 192.168.0.150/24，默认网关192.168.0.1，DNS192.168.0.1与8.8.8.8 |
| libvirt NAT | default / virbr0 | 192.168.122.1/24，DHCP192.168.122.2–254，active/persistent/autostart |

宿主机管理地址、网关、DNS和MAC原值保留，只把三层配置从物理端口迁到 br0；eno2np1 不再单独持有 IPv4。平台默认 `network_type=network`、`default_network=default`，可在创建表单切换桥接，`default_bridge=br0`。virbr0 也是 Linux 网桥，但属于内部 NAT；需要局域网直连时应选 br0。

迁移前进行了配置转换单元检查和 `netplan generate --root-dir` 预演；实际应用先启动180秒自动回滚，重连SSH、平台agent、网关、路由和DNS均成功后才取消回滚。证据见 `evidence/v1.1-bridge-migration.json`。

## 在创建界面配置

1. 目标主机选择 node150；创建虚拟机，网络模式选择“桥接”。
2. 网络来源选择“网桥 br0”。
3. 选择 qcow2/img 磁盘镜像并启用 cloud-init；填写用户及密码或SSH公钥。
4. 保持“使用 DHCP 自动获取地址”勾选时，由局域网DHCP服务分配地址。要固定地址则取消勾选。
5. 填写 **虚拟机自己的** IPv4/CIDR，例如 `192.168.0.201/24`。v1.1.1 默认按联网配置处理：当前 node150 的 br0 在匹配 `192.168.0.0/24` 时自动填入网关 `192.168.0.1` 和 DNS `192.168.0.1, 8.8.8.8`，手填值保留；其他网桥或网段须填写其真实网关和 DNS。示例地址不代表空闲，请先检查并预留地址，勿填写宿主机、网关或其他已使用地址。
6. 只有确实只需同网段通信时，才勾选“仅同网段内网（不配置网关和 DNS）”；该模式不提供跨网段或互联网访问。默认联网模式清空网关或 DNS 会在提交时提示补齐。
7. 创建后任务绑定目标主机；cloud-init 按 MAC 匹配网卡，写入静态地址、默认路由及 DNS。此功能不修改已有 VM 的运行中 IP、路由或 DNS。

NAT模式默认保持DHCP；静态地址只支持“disk + cloud-init + bridge”。ISO安装由系统安装器配置网络，未启用初始化时不注入网络配置。静态校验在磁盘和seed写入之前完成，失败显示在创建任务中。

## API字段与校验

- `network_type`: `network` 或 `bridge`；`network_source`: 目标机返回的网络／网桥名称。
- `ci_dhcp`: 缺省／true保持旧DHCP行为，忽略其他静态字段；false启用下列配置。
- `ci_ip`: 显式 `IPv4/前缀`，前缀1–30，不能网络、广播、组播、回环或未指定地址。
- `ci_isolated`: 缺省／false 为联网配置，最终网关和 DNS 均须存在；true 为显式仅同网段模式，不套用默认值，不生成路由／DNS，同时传非空网关或 DNS 会被拒绝。
- `ci_gateway`: 联网配置必需，必须是同子网另一合法单播地址，不得等于 VM、网络或广播地址；省略时仅从匹配的 `static_network_defaults` 补齐。
- `ci_dns`: 联网配置必需，IPv4 数组或逗号／空白分隔字符串；必须是合法 IPv4，省略时仅从匹配 profile 补齐。

后端不会自动预留路由器DHCP租约，也不保证任意自选地址无冲突。管理员须使用网络规划内已分配给该VM的地址。

## 显式网桥默认值（v1.1.1）

执行端 `/etc/vm-platform/agent.json` 可为网桥配置经管理员确认的默认值；不会从 guest 地址猜测网关，也不会直接复制宿主机的 DNS。当前仅 node150 配置：

```json
{"static_network_defaults":{"br0":{"cidr":"192.168.0.0/24","gateway":"192.168.0.1","dns":["192.168.0.1","8.8.8.8"]}}}
```

只有请求选中同一网桥、guest IP 位于 profile CIDR 内，且 profile 网关位于 guest 自己的 CIDR 内时才补缺项；已填写的合法值优先。profile 的 CIDR 必须是规范网络地址（如 `192.168.0.0/24`，不是 `192.168.0.1/24`）。不匹配或配置无效时不套用，缺项在任何磁盘／seed 写入前拒绝；补入的网关也须通过 guest 地址、网络地址和广播地址校验。`networks` 仅返回实际存在网桥的有效 profile。node30 未配置 profile，需要手填，不将其 Tailscale DNS 自动注入 guest。
profile 的前缀必须为 1–30，且 DNS 列表非空；不符合时整个 profile 不生效。


前端仅清除切换网段／网桥后失效的自动值，保留手填值；提交时不偷偷重填用户已清空的字段。API 仍支持只传 IP，由执行端应用上述匹配规则。DHCP 行为保持兼容。

修复只影响新创建。原先已经生成的 cloud-init/Netplan 不会自动重写；已有 `192.168.0.160` VM 需要另行在 guest 内补上默认路由和 DNS。修复及真实联网验收见 [ACCEPTANCE-1.1.1.md](ACCEPTANCE-1.1.1.md)。

## 网络迁移与独立回退

维护脚本 `deploy/prepare-node150-bridge.py` 是仅用于当前 node150 基线的受保护迁移工具，不是通用节点初始化程序。它校验主机名、管理配置原始哈希、物理MAC、无VM状态，原配置不符即中止。已成功迁移后不能再次 `--apply`。

备份保存在 node150：

```text
/var/lib/vm-platform/backups/node150-bridge-v1/01-netcfg.yaml.original
/var/lib/vm-platform/backups/node150-bridge-v1/prepare-node150-bridge.py
/var/lib/vm-platform/backups/node150-bridge-v1/confirmed.json
```

如需从桥接退回原物理网卡管理方式，应先确认所有桥接VM已正常迁走或停止、没有任务在执行，并通过可恢复的SSH／控制台运行：

```sh
sudo /usr/bin/python3 /opt/vm-platform/deploy/prepare-node150-bridge.py --rollback
```

回退恢复原 Netplan 并重新应用，不删除 default NAT、模板或VM磁盘。平台软件回退和网络回退独立：只停止新平台服务不会自动撤销网桥。回退时不能在仍有业务VM使用br0的情况下断开物理网桥。
