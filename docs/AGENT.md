# VM Platform 执行端（agent）说明

执行端是新服务 `/opt/vm-platform` 的本地执行层，只操作**本机** libvirt，不访问其它主机。
控制端（8089）本地通过命令行客户端、远程通过受限 SSH 连接本机常驻服务：

| 通道 | 命令 | 用途 |
| --- | --- | --- |
| Unix socket | `worker.py --serve` | 常驻（systemd），默认通道；CPU 采样在进程内保留 |
| SSH forced command | `worker.py --ssh` | 受控端只允许 `rpc` 与 `vnc <UUID>` |
| RPC 客户端 | `worker.py --rpc` | 一行 JSON 进出，转发到本机 Unix socket；服务离线即报错 |

实现文件：`worker.py`（传输/协议）、`vmops.py`（本机操作与安全边界）、`ipdiscovery.py`（受限后台地址探测）。仅使用 Python 标准库，
同时兼容 Python 3.6.8（.30，EL7）与 Python 3.12（.150，Ubuntu 24.04）。

## 1. 运行模式

```
worker.py --serve [--config PATH] [--socket PATH]   # 常驻，root 必需
worker.py --rpc [--config PATH]                     # stdin 一行 JSON -> stdout 一行 JSON
worker.py --ssh [--config PATH]                     # 读 SSH_ORIGINAL_COMMAND
worker.py --vnc <UUID> [--config PATH]              # 原始字节双向转发
```

- `--serve`：socket 目录 `0700`、socket 文件 `0600`，并在 Linux 上额外用 `SO_PEERCRED` 校验连接方 uid 必须为 0。
  已存在活动实例时拒绝启动；残留 socket 会被清理；`SIGTERM/SIGINT` 退出时删除 socket。
- `--rpc` / `--ssh rpc`：**stdout 只允许一行 JSON**，转发给常驻 agent，诊断信息写 stderr；不回落为直接执行。
- `--vnc <UUID>`：通过 agent 校验 UUID、名称与回环 VNC 地址，再在本机连接并转发原始字节。
  服务离线或校验失败时 stdout 不输出任何字节，错误写 stderr 并以非 0 退出。
- `--ssh`：`SSH_ORIGINAL_COMMAND` 去掉首尾空白后必须严格等于 `rpc` 或 `vnc <UUID>`，其它一律拒绝
  （退出码 2）。`vnc` 参数不是合法 UUID 时在连 libvirt 之前就拒绝。

退出码：`0` 表示成功完成协议；RPC 业务或 JSON 格式拒绝仍返回 0，以 `ok:false` 判断失败；`2` 用于空／超长 RPC 输入或 SSH 命令／VNC UUID 被拒；`3` VNC 连接失败；`4` 执行端配置不可用。

## 2. 配置

路径取 `VM_PLATFORM_AGENT_CONFIG`，默认 `/etc/vm-platform/agent.json`；也可用 `--config` 覆盖。
示例见 `config/agent.example.json`。除下列字段外的键会被忽略。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `libvirt_uri` | `qemu:///system` | virsh 连接串 |
| `allow_writes` | `false` | **默认只读**。false 时 create/action/config 一律拒绝 |
| `read_only_uuids` | `[]` | 额外禁止写操作的虚拟机 UUID（大小写不敏感） |
| `image_dirs` | `[]` | **只读模板目录**（ISO/cloud image）；为空时 images 为空、create 拒绝 |
| `disk_dirs` | `[]` | **唯一允许新建/扩容/删除磁盘的目录**；为空时相关操作拒绝 |
| `storage_report_dir` | `""` | status 里磁盘容量取哪个目录；留空则用第一个存在的 disk_dirs/image_dirs |
| `default_bridge` | `br0` | 桥接默认值 |
| `default_network` | `default` | libvirt 网络默认值 |
| `default_network_type` | `network` | `network` 或 `bridge` |
| `static_network_defaults` | `{}` | 按网桥配置 `{cidr,gateway,dns:[IPv4]}`，用于静态联网缺项补齐；CIDR 须为规范网络地址且前缀 1–30，网关须在网段内且非网络／广播，DNS 非空；无效配置或非实际网桥不套用 |
| `bridge_ip_discovery` | `false` | 显式启用运行中桥接 VM 的受限后台 ARP 地址发现；仅本机真实网桥有 `/24`–`/30` IPv4 时生效，不修改 guest 或宿主机网络 |
| `default_timezone` | `Asia/Shanghai` | cloud-init 时区 |
| `socket_path` | `/run/vm-platform/agent.sock` | `--serve` 监听路径 |
| `virsh` / `virt_install` / `qemu_img` / `genisoimage` | 同名命令 | 可执行文件路径覆盖；`genisoimage` 为空时自动找 genisoimage/mkisofs |
| `max_vcpus` / `max_memory_gb` / `max_disk_gb` | 256 / 1024 / 20000 | 上限校验 |
| `command_timeout` / `install_timeout` | 120 / 3600 | 秒 |

配置里的目录会统一做 `realpath`，并且必须是绝对路径，否则启动即报错。
**配置文件不存在不会放宽权限**：此时 `allow_writes=false`、目录为空，所有写操作都会被拒绝。

## 3. 协议

请求（一行，UTF-8，最大 4 MiB）：

```json
{"operation": "action", "payload": {"uuid": "…", "action": "start"}}
```

响应（一行）：

```json
{"ok": true, "data": {…}}
{"ok": false, "error": "中文错误信息"}
```

- `operation` 只接受 `status, vms, images, networks, create, action, config, vnc_target`。
- `payload` 缺省为 `{}`，必须是 JSON 对象。
- 业务错误（`vmops.OpError`）返回中文消息；未预期异常返回 `执行端内部错误：…`，都不带堆栈。
- 执行端不会把命令 argv、cloud-init 密码、SSH 私钥等写进 `data` 或 `error`（错误文本会屏蔽已知密码）。

### 3.1 `status` → `data`

```json
{"hostname":"node150","os":"Ubuntu 24.04","cpu_count":16,
 "memory_total":67108864000,"memory_available":51380224000,
 "disk_total":500107862016,"disk_free":268435456000,
 "vm_count":4,"running_count":2,"kvm":true}
```

容量单位均为字节。

### 3.2 `vms` → `data.vms[]`

每个元素：`uuid, name, state, managed_save, id, cpu, max_memory, used_memory, interfaces[], ip_addresses[], disks[], metrics{cpu{percent,text},memory{…},storage{…}}, config{vcpus,memory_gb,primary_disk_target,primary_disk_path,primary_disk_gb,primary_disk_human}, display, has_console`。

- `state`：`running / paused / shut off / saved / in shutdown / pmsuspended / crashed` 等 libvirt 原值（小写），有 managed save 的关机域归为 `saved`。
- `interfaces[]`：`{interface,type,source,model,mac,ips[],ip_source}`；来源为 `agent`、`dhcp`、`neighbor`、`arp` 或空。
- 地址优先读取 qemu-guest-agent → `virsh net-dhcp-leases`（NAT）→ 主机 ARP/neigh；显式启用后可对缺少地址的运行中桥接 VM 异步探测局域网 ARP。仅真实响应可作为地址，不把创建配置中的 IP 当作实测值。
- `ip_status`：`resolved`／`discovering`／`unavailable`／`not_running`；`ip_status_reason` 为简短原因。未探测到不等于 guest 没有网络，详见 NETWORKS.md。
- `metrics.cpu.percent` 依赖常驻 agent 的两次采样，第二次查询起可返回利用率。本地 RPC 与远程 SSH 最终使用同一个私有 socket，因此采样和 ARP 缓存能跨请求保留。

### 3.3 `images` → `data`

```json
{"images":[{"name","path","type":"iso|disk","size","size_human","mtime","virtual_size","format","metadata":{…}}],
 "dirs":["/var/lib/libvirt/images/templates"],
 "disk_dirs":[{"path","name","total","used","free","free_human","exists"}]}
```

只扫描 `image_dirs` 第一层，忽略符号链接与非普通文件；不写数据库，`metadata` 由文件名推断
（`alias, os_family, os_version, image_type, default_username, cloud_init_supported, notes`）。

### 3.4 `networks` → `data`

`{bridges:[…], libvirt_networks:[{name,state}], default_bridge, default_network, default_type, default_source, static_network_defaults:{bridge:{cidr,gateway,dns:[IPv4]}}}`。桥接与 libvirt 网络分别保留独立默认源；仅返回实际存在网桥的有效静态 profile，未配置时为空对象。

### 3.5 `create` payload

```
{kind:"disk"|"iso", name, source, disk_dir, memory_gb, vcpus, disk_gb,
 network_type:"network"|"bridge", network_source,
 cloud_init_enabled, ci_username, ci_password, ci_ssh_key, ci_dhcp, ci_isolated, ci_ip, ci_gateway, ci_dns}
```

- `kind=disk`：`source` 为 `image_dirs` 内的 qcow2/img/raw 云镜像，执行 `qemu-img convert -O qcow2` 到
  `<disk_dir>/<name>.qcow2`，再按 `disk_gb` 扩容（小于镜像虚拟容量直接拒绝）。
- `kind=iso`：`source` 为 `image_dirs` 内的 ISO，`disk_gb` 必填，新盘由 virt-install 创建。
- `cloud_init_enabled` 仅支持 `kind=disk`，生成 `<name>-seed.iso`。默认 DHCP；`ci_dhcp:false` 仅在 disk+cloud-init+bridge 可用，`ci_ip` 必须为可用 IPv4/CIDR（前缀 1–30）。v1.1.1 默认联网：`ci_gateway` 须为同子网另一合法主机地址，`ci_dns` 为非空 IPv4 数组或逗号／空白分隔字符串；缺项仅从目标网桥、guest 地址与网关均匹配的 `static_network_defaults` 补齐，仍缺项则在写盘前拒绝。`ci_isolated:true` 显式仅同网段模式允许省略两者，不应用 profile、不生成路由／DNS，传非空网关或 DNS 则拒绝。全部校验在磁盘目录创建、qemu-img 和 seed 写入前完成；使用同一 MAC 匹配 guest 网卡。已有 VM 不自动重写。
- 新建 VNC 一律 `listen=127.0.0.1`，并带 QEMU Guest Agent channel。
- OS 参数按 `virt-install --version` 分流：`< 4`（如 .30 的 1.5）用 `--os-type linux`；
  `>= 4`（如 .150 的 4.1）用 `--osinfo detect=on,require=off`；版本无法识别时按新版处理（不猜测旧参数）。
- 返回只含安全摘要：`{"uuid","name","kind","summary"}`。
- 失败清理：仅当域未登记时删除本次新建的 qcow2/seed ISO，且必须仍在 `disk_dirs` 内。

### 3.6 `action` payload

`{uuid, action:"start|shutdown|stop|reboot|suspend|resume|delete", remove_disks, force}`

映射：start→`virsh start`、shutdown→`shutdown`、stop→`destroy`、reboot→`reboot`、suspend→`suspend`、resume→`resume`。

`delete`：运行/暂停状态必须 `force=true` 才会 `destroy`；`remove_disks=true` 时**先完成预检再 undefine**：

1. 任一其它虚拟机 `domblklist` 读取失败 → 直接报错，不删除任何文件；
2. 其它虚拟机（按 `domuuid` 排除目标自身）引用的磁盘 → 跳过；
3. `image_dirs` 里的模板：`.iso` 一律跳过（目标自身的 `<name>-seed.iso` 例外），qcow2 等只有同时位于
   `disk_dirs` 时才可能删除；
4. 路径不在 `disk_dirs`、含符号链接、不是普通文件 → 跳过；
5. 目标有 managed save 时先 `managedsave-remove`；
6. undefine 后按预检清单逐个删除（删除前再校验一次）。

返回：`{"uuid","name","action","state":"deleted","removed_count","skipped_count","skipped_reasons":[…],"summary"}`（不含文件名以外的敏感信息）。

### 3.7 `config` payload

`{uuid, vcpus, memory_gb, disk_gb, resize_guest}`

- 必须处于 `shut off`（`saved`/运行/暂停都拒绝）；`resize_guest=true` 直接拒绝（停机不做 guest 扩容）。
- CPU/内存：改 XML 的 `vcpu`/`memory`/`currentMemory`，以 0600 临时文件 `virsh define`，用完立即删除。
- 磁盘：`disk_gb` 小于当前容量 → 拒绝（**禁止缩小**）；大于才 `qemu-img resize`，
  且磁盘必须位于 `disk_dirs`、非符号链接、是普通文件。
- 返回：`{"uuid","name","vcpus","memory_gb","disk_gb","changed":[…],"state","summary"}`。

### 3.8 `vnc_target` payload

`{uuid}` → `{"uuid","name","host":"127.0.0.1","port":5901,"display":1}`。
域未运行、无 VNC、或 `domdisplay` 不是回环地址时返回错误——**不会**改连其它地址。

## 4. 安全边界（实现位置）

| 约束 | 位置 |
| --- | --- |
| 名称/UUID 严格格式；`domname`→`domuuid` 双向核对，拒绝名称冒充 UUID | `vmops.Host.resolve_domain` |
| 模板只读、路径必须落在配置目录、逐级拒绝符号链接与 `..` | `Host._resolve_inside` / `_reject_symlink_components` / `_template_image` |
| 可变磁盘只允许 `disk_dirs` | `Host._mutable_disk_path` |
| 删除前预检、模板/共享盘保护、读取失败即拒绝 | `Host._deletion_plan` / `_other_vm_disk_paths` |
| 磁盘只扩不缩、停机才能改 CPU/内存 | `Host.configure`（协议 operation 仍为 `config`） |
| 命令固定 argv（无 shell） | `run_command` / `Host.run` / `Host.virsh` |
| 密码不外泄（结果只留 summary/name/uuid，错误文本屏蔽密码） | `Host.scrub` / `create` 返回值 |
| cloud-init 文件 0600、临时目录 0700 且用后删除、seed ISO 0600 | `Host._create_seed_iso` |
| 域 XML 临时文件 0600 且用后删除 | `Host._save_domain_xml` |
| 只读模式（`allow_writes=false` / `read_only_uuids`） | `Host._require_writes_allowed` |
| socket 仅 root（0600 + SO_PEERCRED） | `worker.prepare_socket` / `_peer_is_root` |
| SSH 白名单只能用 `rpc` / `vnc <UUID>` | `worker.run_ssh` |

## 5. 与旧 `/opt/vm-panel` 的差异（迁移注意）

1. 旧面板直接在 8088 进程内操作 libvirt，且 VNC 新建用 `listen=0.0.0.0`；执行端固定 `127.0.0.1`。
   旧虚拟机若仍是 `0.0.0.0`，`vnc_target`/`--vnc` 会明确拒绝，需要先在执行端改 XML 的 display listen。
2. 旧面板把镜像元数据存 sqlite；执行端不落库，只按文件名推断。
3. 旧面板允许 `is_allowed_path` 走 `image_dirs + disk_dirs`，新执行端把模板（`image_dirs`）与可变磁盘
   （`disk_dirs`）分开，写操作只认后者。
4. 旧 `delete` 先 undefine 再删文件；新流程先预检、后 undefine、最后逐个再校验删除。
5. 首版为 DHCP；v1.1 增加桥接 cloud-init 静态 IPv4；v1.1.1 将联网所需网关与 DNS 设为必需，并提供经配置的网桥默认值与显式仅同网段模式。不支持通过创建接口修改宿主机地址。
6. 旧代码 `create_from_iso` 用 `--os-type linux`；新代码按 virt-install 版本分流（见 3.5）。

## 6. 部署（.150，Ubuntu 24.04 / Python 3.12）

```bash
install -d -m 0755 /opt/vm-platform /etc/vm-platform
install -m 0644 worker.py vmops.py ipdiscovery.py /opt/vm-platform/
install -m 0755 -d /opt/vm-platform/docs
install -m 0644 docs/AGENT.md docs/IMPLEMENTATION.md /opt/vm-platform/docs/
install -m 0600 config/agent.example.json /etc/vm-platform/agent.json   # 首次安装；不要覆盖已有私有配置
chmod 0600 /etc/vm-platform/agent.json
install -m 0644 deploy/vm-platform-agent.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vm-platform-agent
systemctl status vm-platform-agent --no-pager
ss -xlp | grep vm-platform          # 只有 /run/vm-platform/agent.sock，无 TCP 监听
```

首次安装后须在 .150 配置中明确设 `allow_writes:true`，镜像目录为 templates、磁盘目录为 `/var/lib/libvirt/images/vms`、网络为 `default` NAT，再启动服务。示例默认 false，只复制示例不能执行创建。可用统一 `deploy/install-agent.sh` 等待 socket 就绪。检查应以 root 运行。

依赖（.150）：`libvirt-daemon-system libvirt-clients virtinst qemu-utils genisoimage`。
`virt-install --version` 应为 4.x。

### .30（CentOS 7 / Python 3.6.8）

同法安装 `worker.py`／`vmops.py`／`ipdiscovery.py`／service。仅查询时可使用以下保守配置：

```json
{"allow_writes": false, "image_dirs": ["/var/lib/libvirt/images"], "disk_dirs": ["/home/vm-disks"], "default_network_type": "bridge", "default_bridge": "br0"}
```

当前实际部署为 `allow_writes:true`、磁盘目录 `/home/vm-disks`。首版曾保护全部 14 个原 VM UUID，后续用户明确授权全部解除，当前 `read_only_uuids:[]`；管理能力仍受路径、资源和删除预检约束，更新不得恢复旧名单。
未来接入主机应先只读核对，再按需要明确开启写入并配置保护 UUID；不要根据名称判断保护身份。

### 远程 SSH 通道（转发到节点本机 Unix socket）

`/root/.ssh/authorized_keys` 中：

```
from="192.168.0.30",restrict,command="/usr/bin/python3 /opt/vm-platform/worker.py --ssh" ssh-ed25519 AAAA... vm-platform-controller
```

控制端调用：`ssh -p 22 root@<host> rpc`（JSON 走 stdin/stdout）与 `ssh root@<host> vnc <UUID>`（原始字节）。
`restrict` 关闭端口转发与 pty，避免绕过白名单。

### 回滚

```bash
systemctl disable --now vm-platform-agent
rm -f /etc/systemd/system/vm-platform-agent.service
systemctl daemon-reload
rm -rf /opt/vm-platform /etc/vm-platform /run/vm-platform
```

旧 `/opt/vm-panel` 与 8088 未改动，回滚只影响新服务。执行端不修改 Netplan、不新增 TCP 监听。

## 7. 验收（执行端自测）

以下 RPC 命令须以 root 运行，且 `vm-platform-agent.service` 已 active；它们会通过私有 socket 请求常驻服务。

```bash
# 1) 只读能力
printf '%s\n' '{"operation":"status","payload":{}}' | python3 /opt/vm-platform/worker.py --rpc
printf '%s\n' '{"operation":"vms","payload":{}}'    | python3 /opt/vm-platform/worker.py --rpc
# 2) UUID 强校验（名称应被拒绝）
printf '%s\n' '{"operation":"action","payload":{"uuid":"test-vm","action":"start"}}' | python3 worker.py --rpc
# 3) 越界路径（应报“必须位于…目录内”，且无 qemu-img 调用）
printf '%s\n' '{"operation":"config","payload":{"uuid":"<UUID>","vcpus":2,"memory_gb":2,"disk_gb":99}}' | python3 worker.py --rpc
# 4) 模板保护：在临时 VM 上 delete + remove_disks，确认 image_dirs 内文件仍在
# 5) 控制台：vnc_target 必须回 127.0.0.1
printf '%s\n' '{"operation":"vnc_target","payload":{"uuid":"<UUID>"}}' | python3 worker.py --rpc
# 6) SSH 白名单
SSH_ORIGINAL_COMMAND='bash -lc id' python3 worker.py --ssh; echo "exit=$?"   # 期望 2
```

单元测试在包含 `controller.py`、`worker.py`、`vmops.py`、`ipdiscovery.py`、`tests/` 与 `tools/` 的完整源码目录执行（不接触真实 libvirt）；仅按上方精简执行端清单安装时不包含测试：

```bash
cd /path/to/full-vm-platform-source && python3 -m unittest discover -s tests -v
```

## 8. 已知限制

- `metrics.cpu.percent` 需要常驻 agent 内两次采样，刚启动时显示“采样中”。
- `vms` 对每台虚拟机执行多次 virsh（dominfo/domuuid/domiflist/domifaddr/domstats/domblklist），
  虚拟机很多时单次响应会变慢；控制端应缓存并做超时。
- 删除只检查“其它虚拟机直接引用的磁盘路径”，不解析 qcow2 backing chain；模板作为 backing file
  的极端场景依赖 `image_dirs` 只读保护，部署时应把模板单独放一个目录。
- 域 XML 里缺失 `vcpu`/`memory`/`currentMemory` 节点时直接拒绝修改，不做猜测性插入。
- `create` 的 `disk_dir` 必须在配置 `disk_dirs` 白名单内且无符号链接；界面优先使用 `images` 响应提供的目标主机目录。
- 允许目录会解析为真实路径；客户端须提交接口返回的规范路径，别名或符号链接路径会被拒绝。
