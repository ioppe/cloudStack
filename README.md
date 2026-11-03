# 云栈虚拟机平台

面向单管理员的中文 KVM 多宿主机管理平台。统一入口 `http://192.168.0.30:8089/`，旧面板 `:8088` 保留。控制端与执行端只依赖 Python 标准库，分别兼容现有 CentOS 7 / Python 3.6 和 Ubuntu 24.04 / Python 3.12。

当前版本 **1.1.0**。源码仓库：https://github.com/ioppe/cloudStack 。更新记录见 [CHANGELOG.md](CHANGELOG.md)。

- 管理员登录与持久会话；顶栏可修改密码，成功注销全部会话；密码使用 PBKDF2 哈希存储，写请求有 CSRF 校验。
- 主机登记、编辑、检测、解除纳管；各主机独立显示 CPU、内存、磁盘与 VM 状态。
- 按主机创建 ISO/磁盘镜像虚拟机，起停、重启、暂停恢复、配置与扩容、删除。
- NAT／桥接创建模式可选，桥接 cloud-init 支持虚拟机静态 IPv4/CIDR、网关和 DNS；node150 同时保留 br0 与 default NAT。
- 镜像和存储按执行端目录配置隔离；网页 VNC 控制台通过已有 SSH 通道访问宿主机回环端口。
- SQLite 记录异步任务与安全结果摘要；服务中断后的未完成任务标为待核对的失败状态。

## 文件

| 路径 | 用途 |
|---|---|
| `controller.py` | HTTP 登录、主机路由、持久任务与控制台代理 |
| `worker.py`、`vmops.py` | 常驻 Unix socket agent、CLI 客户端、libvirt 与文件操作 |
| `static/` | 中文管理页面与 noVNC 离线资源 |
| `config/*.example.json` | 无私密信息的配置示例 |
| `deploy/` | systemd 服务与安装脚本 |
| `tests/` | 登录、主机隔离、目录、命令参数、失败处理检查 |
| `tools/bootstrap.py` | 一次性生成私有配置和随机管理员凭据 |
| `docs/` | 接口、部署、节点接入与验收记录 |

安装与回退见 [DEPLOY.md](docs/DEPLOY.md)，网络与静态IP见 [NETWORKS.md](docs/NETWORKS.md)，执行端见 [AGENT.md](docs/AGENT.md)，API 见 [IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。首版历史验收见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)，v1.1增量验收见 [ACCEPTANCE-1.1.md](docs/ACCEPTANCE-1.1.md)。

## 本地检查

```sh
python3 -m unittest discover -s tests -v
```

浏览器回归：安装 `agent-browser` 后运行 `python3 tests/e2e_browser.py --guest-public-key /path/to/temporary.pub --report docs/evidence/e2e-browser.json`。此流程会在 `.150` 创建专用 `platform-accept-20261103`，不得对已有业务 VM 运行。真实生命周期、故障与清理场景见 `tests/e2e_platform.py`、`e2e_fault.py`、`e2e_environment.py` 以及验收记录。

v1.1 浏览器回归使用 `tests/e2e_bridge_password.py bridge|password`，桥接阶段须提供提前检查空闲的临时IPv4/CIDR与scratch中的一次性SSH key，会创建并清理专用 node150 VM；改密阶段实际修改、验证失效并恢复交付密码。不要在生产业务VM上执行。

`?demo=1` 仅供静态界面演示，使用虚构数据；正式管理与所有验收使用无此参数的统一入口。

私有运行配置位于服务器 `/etc/vm-platform/`，数据库位于 `/var/lib/vm-platform/`。默认密码由 `tools/bootstrap.py` 随机生成，交付时保存在管理员专用文件 `ADMIN-CREDENTIALS.txt`（权限 0600）。该文件和运行配置不可包含在公开源码归档中。

Web 入口当前按用户要求使用内网 HTTP；无跨网访问部署。本项目不提供虚拟机在线迁移、HA 或共享存储编排。
