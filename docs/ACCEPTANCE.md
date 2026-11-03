# 多宿主机虚拟机管理平台首版验收记录

> 此记录与 evidence/e2e-*.json 为1.0首版历史验收。v1.1根据后续需求新增桥接静态IP、改密界面和GitHub同步，当前增量验证见 ACCEPTANCE-1.1.md。旧证据保持原始结果，首版“Netplan未改／无外部推送”不代表v1.1当前状态。

验收日期：2026-11-03。依据已批准目标《多宿主机虚拟机管理平台首版》，九项验收全部满足，无未完成项或外部阻塞。

入口：<http://192.168.0.30:8089/>。管理员用户名 `admin`；最终随机口令独立交付在工作区 `ADMIN-CREDENTIALS.txt`（0600），不包含在源码包中。验收期间已轮换旧口令并使旧会话失效，新口令的登录／退出已验证。

## 九项验收

| # | 验收项 | 结果与实际观察 | 证据 |
|---|---|---|---|
| 1 | 平台访问与持续运行 | **满足**。正式入口提供中文主机、虚拟机、镜像、任务与控制台界面；.30 控制端及两机 agent 全部 active/enabled。标准库源码、配置示例、部署／接入／回退说明齐全。 | [最终环境](evidence/e2e-environment.json)、[页面证据](evidence/browser-gallery.html)、[部署说明](DEPLOY.md)、[执行端说明](AGENT.md) |
| 2 | 管理员登录 | **满足**。正确凭据登录、退出成功，错误凭据和未登录管理请求返回 401；CSRF／同源控制台检查通过。当前口令及私钥在前端与普通服务日志的命中均为 0，私有配置／密钥／socket 为 0600。 | [线上登录验收](evidence/e2e-readiness.json)、[安全核验](evidence/e2e-security.json)、[单元日志](evidence/unit-final-local.log) |
| 3 | 两台主机真实纳管 | **满足**。node30=k8s-node03、node150=sky，均为 80 逻辑 CPU，内存分别约 125.4／125.5 GiB。最终主机页均在线，VM 数量分别 14／0；资源来自对应主机。新增／编辑／检测／移除临时登记已通过，node30 的显示名在浏览器中修改后恢复，地址未改变。 | [主机 API](evidence/e2e-readiness.json)、[最终主机页面](evidence/browser-hosts-final.png)、[最终浏览器](evidence/e2e-browser-final.json) |
| 4 | 按主机隔离 | **满足**。node30 的 14 行 VM 切换为 node150 空表，镜像、磁盘目录及 NAT 来源绑定 node150。后端按 host ID + VM UUID 路由，任务提交时捕获主机；同名隔离不依赖 VM 名称。未知主机 404、目录外 `/etc/passwd` 来源产生 node150 失败任务且无落盘。 | [浏览器切换与创建](evidence/e2e-browser.json)、[越界拒绝](evidence/e2e-path-failure.json)、[主机／UUID 单元检查](evidence/unit-final-python36.log) |
| 5 | 完整 VM 管理 | **满足**。从官方 Ubuntu .img（qcow2 格式）经 UI 创建；临时 VM 获得 `192.168.122.29`。暂停、恢复、重启、正常关机、启动、强制停止均通过；重启前后 guest boot ID 改变。停机从 2 CPU／2 GiB／12 GiB 调为 3 CPU／4 GiB／16 GiB，启动后 guest 识别 3 CPU。ISO 的 4.1／1.5 参数分流及错误来源拒绝自动化通过。最终删除 VM 与 2 个专用文件。 | [真实生命周期](evidence/e2e-lifecycle.json)、[ISO 定向日志](evidence/unit-iso-errors.log)、[清理结果](evidence/e2e-cleanup.json) |
| 6 | 镜像与控制台 | **满足**。node150 模板页显示真实格式／容量，创建表单实际读取该机 templates、vms 目录与 default NAT。平台 VNC 显示“已连接”，主机 node150、VM UUID 与画布均核验；实际 VNC 仅监听 127.0.0.1。 | [模板页面](evidence/browser-node150-images.png)、[创建表单](evidence/browser-create-form.png)、[VNC 页面](evidence/browser-vnc-connected.png)、[安全元数据](evidence/e2e-security.json) |
| 7 | 任务及故障反馈 | **满足**。任务显示主机、VM、执行／成功／失败及原因。真实停止 .150 新 agent 后该机查询 502，临时 VM 暂停任务失败且保留 node150／UUID；同时 .30 仍独立可查 14 台 VM、3 台运行。finally 恢复服务后重新在线，失败操作未改变 VM 状态。最终任务页同时显示成功与故障原因。 | [任务提交](evidence/browser-task-submitted.png)、[故障／恢复](evidence/e2e-agent-fault.json)、[最终任务页](evidence/browser-tasks-final.png) |
| 8 | 新主机准备 | **满足**。KVM、QEMU 8.2、libvirt 10、virt-install 4.1、genisoimage、模板和存储目录可用；default NAT active/persistent/autostart。专用受限 SSH 真实可调用 agent，任意 `id` 命令被拒绝（exit 2，stdout 为空）。两机 agent 开机启动，无新增 agent TCP 端口。 | [准备记录](HOST-150.md)、[受限连接核验](evidence/e2e-security.json)、[最终环境](evidence/e2e-environment.json) |
| 9 | 既有兼容与清理 | **满足**。8088 HTTP 200、vm-panel active；旧 server.py／config.json／vm-panel.db SHA-256 与初始基线逐字一致；14 台 VM 的 UUID、原始状态、inactive XML SHA-256 全部一致。三种 Python 的完整 60 项及新增 ISO 定向检查通过；浏览器覆盖切换／创建／任务／控制台；临时 VM、qcow2 和 seed ISO 已清理。 | [初始 VM 基线](evidence/old-domains-baseline.json)、[最终逐项比较](evidence/e2e-environment.json)、[删除任务](evidence/e2e-cleanup.json)、[浏览器报告](evidence/e2e-browser.json) |

## 专用临时 VM

| 项目 | 观察值 |
|---|---|
| 目标 | node150 / 192.168.0.150 |
| 名称 | platform-accept-20261103 |
| UUID | 506cc484-fdda-494e-9681-5da8331b7e27 |
| 创建任务 | f6eb85b85a38459ca743c09330992097 |
| 初始资源 | 2 CPU、2 GiB 内存、12 GiB 独立 qcow2 |
| NAT 地址 | 192.168.122.29 |
| guest 验证 | vmcheck，hostname 与 VM 名称相同，cloud-init 完成，SSH 一次性公钥登录成功 |
| 重启 boot ID | 6dfb04dc-c99e-4eaa-9f05-0d03ed2e69fe → 9ba8b3eb-ed62-49d5-9c5f-d1bd3c43aa25 |
| 配置后 | 3 CPU、4 GiB、16 GiB；guest 识别 3 CPU |
| 删除任务 | ce7c5881c1bf49119faaaf6ae0284828 |
| 删除结果 | domain 不存在，专用 qcow2 + seed ISO 删除 2 个，跳过 0，临时文件残留 0 |

一次性 guest 私钥只留在会话 scratch，由 SSH 客户端本地使用，未搬运到宿主机或归档。模板与 NAT 保留供管理员继续使用。

## 自动化与浏览器记录

完整单元检查在 macOS Python 3.13.7、.30 `/usr/bin/python3` 3.6.8、.150 Python 3.12.3 分别运行，三端均为 60/60、无失败或跳过。之后新增一个 ISO 错误来源用例，三端分别定向执行 `CreateCommandTests` 的 5 项并全部通过；因此当前源码中的 61 项用例均有通过证据。最后一处登录按钮修复单独通过 node 语法复查，console 模块语法同样通过。

- [本机完整日志](evidence/unit-final-local.log)
- [Python 3.6 完整日志](evidence/unit-final-python36.log)
- [Python 3.12 完整日志](evidence/unit-final-python312.log)
- [ISO 新增／相关 5 项日志](evidence/unit-iso-errors.log)
- [JavaScript 语法与脚本编译](evidence/js-syntax.log)
- [浏览器截图集](evidence/browser-gallery.html)

早期 `unit-full-*` 日志保留了整合阶段失败及修复前快照，最终结论以 `unit-final-*`、`unit-iso-errors.log` 与各 `e2e-*.json` 的 `passed:true` 为准。实际写操作仅发生在上述专用 node150 VM；原有 VM 未执行起停／修改／扩容／迁移／删除。

复现主要命令（先按部署说明准备受限连接、私有凭据、空闲测试节点与一次性 guest key）：

```sh
python3 -m unittest discover -s tests -v
python3 tests/e2e_platform.py readiness --report docs/evidence/e2e-readiness.json
python3 tests/e2e_platform.py failure --report docs/evidence/e2e-path-failure.json
python3 tests/e2e_browser.py --guest-public-key /path/to/temporary.pub --report docs/evidence/e2e-browser.json
python3 tests/e2e_platform.py lifecycle --guest-key /path/to/temporary --report docs/evidence/e2e-lifecycle.json
python3 tests/e2e_fault.py --report docs/evidence/e2e-agent-fault.json
python3 tests/e2e_security.py --report docs/evidence/e2e-security.json
python3 tests/e2e_platform.py cleanup --report docs/evidence/e2e-cleanup.json
python3 tests/e2e_environment.py --report docs/evidence/e2e-environment.json
python3 tests/e2e_browser_status.py --report docs/evidence/e2e-browser-final.json
```

这些真实验收脚本会创建／修改／删除专用临时 VM，并短暂停止／恢复 node150 新 agent；只在测试范围与命名保护满足时执行，不对业务 VM 运行。ISO 安装器未在真实 VM 中手动安装，按合同使用参数与错误处理自动化核验。

## 保留状态与交付

.30 新源码 `/opt/vm-platform`、私有配置 `/etc/vm-platform`、SQLite 状态 `/var/lib/vm-platform`；旧 `/opt/vm-panel`、8088 与旧服务保留。原有 14 个 UUID 已进入新 agent 的 `read_only_uuids`。

.150 保留 `/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img`、`/var/lib/libvirt/images/vms`、default NAT、agent 及开机启动配置。Netplan SHA-256 保持 `970708daf1c44e6adfe1d2df7ae25e8b9bdb3787bad36d87da8bdbaf2be7a4ea`，`eno2np1=192.168.0.150/24`、默认网关 `192.168.0.1` 不变。

源码包为 `releases/vm-platform-1.0.0-source.tar.gz`，含配置示例、部署脚本、源码、noVNC 许可、文档与证据；排除管理员明文凭据、运行配置、私钥、数据库及缓存。回退命令详见 [DEPLOY.md](DEPLOY.md)。未向外部代码仓库提交或推送。
