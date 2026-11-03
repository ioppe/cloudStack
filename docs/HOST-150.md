# HOST-150 虚拟化准备（192.168.0.150）

> 本文是1.0环境准备阶段的历史快照。v1.1按后续请求已新增物理br0并将管理地址／网关／DNS原值迁到br0，default NAT保留。当前网络与回退说明见 NETWORKS.md，实际迁移证据见 evidence/v1.1-bridge-migration.json。下文“未改Netplan”仅描述首版准备阶段。

本文记录多宿主机平台新节点 192.168.0.150 的虚拟化前置准备：已安装组件、目录与权限、
libvirt NAT 网络、公开 Ubuntu cloud image 模板，以及回滚方式。执行端服务（worker/agent）由
主会话另行部署，本文不涉及。

- 采集时间：2026-11-03（UTC）05:36–05:43
- 主机：Ubuntu 24.04.5 LTS，kernel 6.8.0-147-generic，80 vCPU / 125 GiB RAM / 1.8T 根盘，`/dev/kvm` 就绪
- 证据日志：`$PI_SCRATCH_DIR/host150-prepare.log`（本机采集会话）

## 1. 网络基线（未改动物理网卡与 Netplan）

基线在安装前采集，安装后复核哈希一致：

| 项 | 值 |
| --- | --- |
| `/etc/netplan/01-netcfg.yaml` sha256 | `970708daf1c44e6adfe1d2df7ae25e8b9bdb3787bad36d87da8bdbaf2be7a4ea` |
| 聚合哈希（排序后 sha256sum 再 sha256） | `2bf665a29df4b34ecc51260c20eea48e5805df52bc59c41f6a55a7761fc9b71e` |

安装前后两次取值完全相同，Netplan 未改。

路由与地址（安装后）：

```
eno1np0   DOWN
eno2np1   UP       192.168.0.150/24  fe80::ae1f:6bff:feeb:a7b1/64
virbr0    DOWN     192.168.122.1/24            # libvirt NAT，无客户机时不 UP，属正常
default via 192.168.0.1 dev eno2np1
192.168.0.0/24 dev eno2np1 scope link src 192.168.0.150
192.168.122.0/24 dev virbr0 scope link src 192.168.122.1
```

DNS 仍由 systemd-resolved 管理（127.0.0.53），未改。eno2np1 的 192.168.0.150/24 与网关
192.168.0.1 保持原状。

## 2. 安装的软件包

`apt-get update` 后安装（`--no-install-recommends`；`qemu-kvm` 在 24.04 无 candidate，由
`qemu-system-x86` 提供同等能力）：

| 包 | 版本 |
| --- | --- |
| qemu-system-x86 | 1:8.2.2+ds-0ubuntu1.18 |
| qemu-utils | 1:8.2.2+ds-0ubuntu1.18 |
| libvirt-daemon | 10.0.0-2ubuntu8.19 |
| libvirt-daemon-system | 10.0.0-2ubuntu8.19 |
| libvirt-clients | 10.0.0-2ubuntu8.19 |
| virtinst | 1:4.1.0-3ubuntu0.1 |
| genisoimage | 9:1.1.11-3.5 |
| dnsmasq-base | 2.91-0ubuntu0.24.04.2 |

`dnsmasq-base` 是补装的：`--no-install-recommends` 会漏掉它，而 libvirt 启动 NAT 网络必须有
`dnsmasq` 二进制，否则 `virsh net-start` 报 `Unable to find 'dnsmasq' binary in $PATH`。
只装 `dnsmasq-base`（纯二进制，不启用系统级 dnsmasq 服务，不占用 0.0.0.0:53）。

服务状态：

```
libvirtd          active/enabled
libvirtd.socket   active/enabled
libvirt-guests    active/enabled
```

`virsh -c qemu:///system version` → hypervisor QEMU 8.2.2；`nodeinfo` → 80 CPU / 131617916 KiB；
`domcapabilities` → `<domain>kvm</domain>`、`<arch>x86_64</arch>`、`<machine>pc-i440fx-noble-v2</machine>`。

## 3. 目录与权限

```
drwx--x--x root:root  /var/lib/libvirt/images           # libvirt 默认 0711，保持原样
drwxr-xr-x root:root  /var/lib/libvirt/images/templates # 模板（只读来源）
drwxr-xr-x root:root  /var/lib/libvirt/images/vms       # 虚拟机磁盘目录
-r--r--r-- root:root  templates/ubuntu-24.04-server-cloudimg-amd64.img
-rw-r--r-- root:root  templates/SHA256SUMS.noble
```

libvirt-qemu（uid 64055，组 kvm）对 templates 有 r-x，实测可读取镜像头
（`head -c 4` 返回 `Q F I 373`，即 QFI\xfb）。

AppArmor 已启用。模板放在 `/var/lib/libvirt/images/` 下是 virt-aa-helper 的 canonical 白名单
（`/etc/apparmor.d/usr.lib.libvirt.virt-aa-helper` 第 58–59 行允许 `/var/lib/libvirt/images/** r`），
因此 VM 启动时生成的 per-domain profile 无需额外 `-R` 规则。`virt-aa-helper` 位于
`/usr/lib/libvirt/virt-aa-helper`（不在 $PATH，属正常）。

`qemu.conf` 的 `user`/`group` 保持注释（Debian/Ubuntu 默认由 root 运行 qemu），目录 0755、
文件 0644/0444，root 与 libvirt-qemu 均可读，未修改 qemu.conf。

## 4. libvirt NAT 网络

未冲突证据（创建前）：不存在 192.168.122.0/24 的路由或接口；无 docker/podman 及桥接设备；
仅 systemd-resolved 监听 127.0.0.53/54:53。

```
virsh net-list --all
 Name      State    Autostart   Persistent
 default   active   yes         yes
```

XML 摘要：`forward mode='nat'`（端口 1024-65535），bridge `virbr0`，
`ip address='192.168.122.1' netmask='255.255.255.0'`，DHCP 范围 `192.168.122.2–192.168.122.254`。
`virsh net-autostart default` 已设置（默认包已定义 default，此处只补 autostart 并启动）。

运行时证据：

- dnsmasq 仅监听 `192.168.122.1:53`（不占 0.0.0.0）
- `iptables -t nat -S` 含 `LIBVIRT_PRT -s 192.168.122.0/24 ! -d 192.168.122.0/24 ... MASQUERADE`
- `net.ipv4.ip_forward = 1`（由 libvirt 在网络启动时设置；`/etc/sysctl.conf` 该行为注释状态，
  重启后随 default 网络自启重新置位）

## 5. 模板镜像

| 项 | 值 |
| --- | --- |
| 绝对路径 | `/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img` |
| 来源 | `https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img`（HTTPS） |
| sha256（实测 = 同站 `SHA256SUMS`） | `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94` |
| 格式 | qcow2 |
| 虚拟大小 | 3.5 GiB (3758096384 bytes) |
| 实际占用 | 593 MiB (621284352 bytes) |
| 权限 | 0444 root:root（只读模板） |
| 清单文件 | 同目录 `SHA256SUMS.noble`（0644，保留作证据） |

说明：`noble-server-cloudimg-amd64.img` 这种旧命名在 noble 已不存在（会 404），正确文件名是
`ubuntu-24.04-server-cloudimg-amd64.img`。

选用 24.04 而非 22.04：主机与执行端均为 24.04 / Python 3.12，且本机原先无 22.04 镜像可沿用。
若验收要求 22.04，需另下载 `jammy` cloud image 并按其 `SHA256SUMS` 复核。

环境准备阶段未创建任何虚拟机（当时 `virsh list --all` 为空）。后续平台通过
`qemu-img convert -O qcow2` 从模板生成独立磁盘并扩容，不使用 backing overlay，也不改写模板；最终临时资源与状态见 `ACCEPTANCE.md`。

## 6. 未开放的对外面

- 无新增对外 TCP 监听：`ss -lntp` 仅 sshd 22（IPv4/IPv6），libvirt 走 Unix socket
- 未创建/启动任何 VM，故无 VNC 端口；后续 VM 的 VNC 必须 `listen=127.0.0.1`
- virbr0 仅内部 NAT，未桥接 eno2np1，未改 Netplan

## 7. 回滚

按需逐条执行（不触碰 Netplan 与物理网卡）：

```
# 1) 停用并取消 NAT 网络自启
virsh net-destroy default
virsh net-autostart default --disable

# 2) 停用服务
systemctl disable --now libvirtd.socket libvirtd libvirt-guests

# 3) 卸载软件包（保留配置文件时去掉 --purge）
apt-get purge -y virtinst genisoimage libvirt-clients libvirt-daemon-system \
  qemu-utils qemu-system-x86 dnsmasq-base
apt-get autoremove -y

# 4) 删除目录与模板（仅在确认无 VM 后）
rm -rf /var/lib/libvirt/images/templates /var/lib/libvirt/images/vms
```

v1.1之后须先按 NETWORKS.md 完成独立网络回退，才应复核 `/etc/netplan/01-netcfg.yaml` 的 sha256 恢复为
`970708daf1c44e6adfe1d2df7ae25e8b9bdb3787bad36d87da8bdbaf2be7a4ea`。
