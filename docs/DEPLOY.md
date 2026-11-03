# 部署、接入与回退

## 拓扑和目录

- `.30:8089`：`vm-platform.service`，统一中文 Web/API 入口；`vm-platform-agent.service` 管理本机。
- `.150`：`vm-platform-agent.service`，只监听 `/run/vm-platform/agent.sock`，无额外 TCP 管理接口。
- 控制端通过专用 SSH key 调用受限的 `rpc` 或 `vnc <UUID>` 命令；VNC 新建时仅监听宿主机 `127.0.0.1`。
- 源码 `/opt/vm-platform`；配置与密钥 `/etc/vm-platform`（0700）；数据库 `/var/lib/vm-platform`（0700）。
- `.30:/opt/vm-panel` 与 `vm-panel.service` 为旧系统，安装脚本不修改。

## 安装控制端

1. 将本目录源码（排除凭据、数据库、`__pycache__`）复制到 `.30:/opt/vm-platform`。无 pip/npm 运行依赖，noVNC 使用随包静态资源。
2. 生成随机管理员凭据和配置：

   ```sh
   python3 tools/bootstrap.py --output /etc/vm-platform/controller.json --credentials /root/vm-platform-credentials.txt
   ```

   生成器拒绝覆盖已有文件。密码哈希只在首次初始化时进入 SQLite；服务重启不会重置密码、主机登记或登录会话。凭据文件仅管理员可读。
3. 按 `config/agent.example.json` 准备本机 `/etc/vm-platform/agent.json`：URI `qemu:///system`，镜像 `/var/lib/libvirt/images`，VM 目录 `/home/vm-disks`，默认桥接 `br0`。保守安装可设 `allow_writes:false`；当前部署为 `allow_writes:true`，并将 `docs/evidence/old-domains-baseline.json` 中全部 14 个原 VM UUID 写入 `read_only_uuids`，禁止改写这些既有 VM。数据库与旧面板分离。
4. 在控制端生成专用 SSH key：

   ```sh
   install -d -m 700 /etc/vm-platform
   ssh-keygen -t ed25519 -N '' -C vm-platform-controller -f /etc/vm-platform/id_ed25519
   ```

5. 将从可信管理通道核对的节点主机公钥写到 `/etc/vm-platform/known_hosts`。不禁用主机密钥校验；重装后的节点须重新核对指纹。
6. 从节点管理连接执行 `python3 tools/authorize-agent.py --controller 192.168.0.30`，stdin 传入控制端 `.pub` 公钥。它只追加该条受限 key，不改其他 key，不复制私钥。仅允许来自控制端 IP，禁止端口转发、PTY、任意 shell。
7. `sh deploy/install-agent.sh`，再 `sh deploy/install-controller.sh`。确认两个 unit active/enabled，访问 `:8089/health` 和登录页。

## 接入 Ubuntu 24.04 节点

1. 在目标机安装 `qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients virtinst genisoimage`，启用 libvirtd。当前 `.150` 软件源没有 qemu-kvm 包候选，使用 qemu-system-x86。
2. 核对 `/dev/kvm` 与 `virsh -c qemu:///system list --all`。准备 `/var/lib/libvirt/images/templates`、`/var/lib/libvirt/images/vms`；node150另有独立 `/home` 大盘，新增专用 `/home/vm-disks`。先确认 `/home` 持久挂载，再 `install -d -m 0755 /home/vm-disks`，QEMU用户需有目录遍历权限；不要将整个 `/home` 加入白名单或放宽无关用户目录权限。
3. 首次接入先启用本机 libvirt `default` NAT 并 `virsh net-autostart default`，检查地址段无冲突。v1.1 的 node150 已按用户后续请求增加 br0（eno2np1为端口），管理IP／网关／DNS原值迁到br0；保留NAT，详细步骤与独立网络回退见 NETWORKS.md。不要对其他节点原样套用本节点专用迁移脚本。
4. 复制 `worker.py`、`vmops.py`、`deploy/` 到 `/opt/vm-platform`；根据示例设置 `/etc/vm-platform/agent.json`，URI `qemu:///system`，`image_dirs:["/var/lib/libvirt/images/templates"]`、`disk_dirs:["/var/lib/libvirt/images/vms","/home/vm-disks"]`。创建表单分别显示每个目录的真实剩余容量，node150的 `/home` 当前约6.9 TiB可用；原目录仍为默认，按需选择大盘。网络为 `default`、类型 `network`；明确设 `allow_writes:true` 才能创建和管理新VM，示例默认false。
5. 执行端 `sh deploy/install-agent.sh`。从已信任管理通道安装受限公钥、核对主机 key；控制端的私钥始终留在控制端。
6. 平台“主机 → 登记主机”填写唯一标识、名称、地址、root/已授权用户和 SSH 端口；检测通过后管理资源。此步骤只是登记，软件和网络初始化仍按上述步骤完成。

node150 当前私有 agent 配置继续使用 `default_network_type:"network"`、`default_network:"default"`，同时设 `default_bridge:"br0"`。新建VM可在界面切换NAT或桥接，静态地址仅注入桥接guest，不修改宿主机。

node150 使用独立 `/home` 存储时，安装挂载依赖：`install -d /etc/systemd/system/vm-platform-agent.service.d`，将 `deploy/node150-home-storage.conf` 安装为该目录的 `home-storage.conf`，再执行 `systemctl daemon-reload` 和 `systemctl restart vm-platform-agent.service`。此配置让 agent 依赖 `home.mount`，防止大盘未挂载时写入系统盘。已有镜像和磁盘不迁移；移除存储选项前确认无VM使用该目录，不删除目录中的业务磁盘。

## 运行与操作

```sh
systemctl status vm-platform vm-platform-agent
journalctl -u vm-platform -u vm-platform-agent --since '10 minutes ago'
curl --noproxy '*' http://127.0.0.1:8089/health
```

平台任务按主机串行执行；任务执行期间拒绝编辑连接或解除该主机纳管。前端刷新不会取消任务。重新启动控制端后，原执行中的任务显示失败并提示核对实际状态，不会自动重试创建或删除。

配置/扩容要求虚拟机关机；磁盘只允许增加。镜像位于目标宿主机，目录互不共享；主机切换会重新加载镜像、存储和网络。删除登记不会删除或停止虚拟机；删除虚拟机需要单独确认，可选择是否删除其允许目录内的专用磁盘。

顶栏管理员旁“修改密码”输入当前密码、新密码和确认值；新密码12–200字符，成功后全部登录会话失效，须重新登录。对应接口 `POST /api/password`，需会话与CSRF。管理员手动修改后须自行安全保管新口令，浏览器不会更新本地凭据文件。SQLite中的密码哈希优先于首次初始化配置，重启不会恢复旧密码。

本次部署可从本机运行 `python3 tools/rotate_password.py` 生成新随机口令、调用改密接口、更新本地 0600 凭据文件，并经受信 SSH 同步私有配置中的初始化哈希。程序不输出口令。服务以后优先使用 SQLite 中的密码哈希，初始化配置不会在重启时重置账户。

## 回退

以下只停止新平台服务，libvirt 与现有 VM 不停止：

```sh
# 控制端 .30
systemctl disable --now vm-platform.service vm-platform-agent.service
# 新节点 .150
systemctl disable --now vm-platform-agent.service
```

继续使用 `http://192.168.0.30:8088/`。保留 `/etc/vm-platform`、`/var/lib/vm-platform` 和源码以便恢复；恢复时 enable --now 对应 unit。需要彻底解除接入时，人工从 `.150:/root/.ssh/authorized_keys` 仅移除带 `vm-platform-controller` 标识的本平台公钥，其他 key 保持不变。

不要为回退删除 `/var/lib/libvirt/images/vms`、模板、虚拟机或停用 libvirtd/default NAT。只有确认无任务运行且没有平台创建的 VM 使用相关资源后，才考虑移除平台软件。禁止对旧面板目录执行同步删除或覆盖。

br0 网络迁移与平台软件回退相互独立；只停止新平台服务不会撤销网桥。独立网络回退步骤和前提见 `NETWORKS.md` 的“网络迁移与独立回退”。
