# 1.1.0 增量验收记录

日期：2026-11-03。需求：node150支持桥接及桥接VM静态IP、管理员界面修改密码、源码同步指定GitHub仓库。正式入口仍为 <http://192.168.0.30:8089/>。

## 功能与观察证据

### node150桥接与静态地址：通过

- 新建物理 `br0`，`eno2np1` 作为桥端口。宿主机地址保留 `192.168.0.150/24`，网关保留 `192.168.0.1`，DNS保留 `192.168.0.1` 与 `8.8.8.8`；default NAT active/persistent/autostart，仍为默认模式。
- 迁移前通过4项单元检查及 `netplan generate --root-dir` 预演。实际迁移先备份和启动180秒自动回滚，独立SSH与平台连接、路由、DNS和网关确认后取消回滚。备份及独立回退见 [NETWORKS.md](NETWORKS.md)。
- 实时网络接口分别返回 `default_bridge=br0`、`default_network=default`，node150默认类型为network。node30原有br0同样核对存在，不改动原有虚拟机。
- 浏览器从正式创建表单选择 bridge/br0，取消DHCP，填写临时VM静态 `192.168.0.248/24`、网关 `.1`、DNS `.1,8.8.8.8`。该地址使用前已3次ARP探测无冲突，仅用于临时验收。
- guest `platform-bridge-accept-20261103` 的 `ens3` 实际获得上述静态地址／默认路由／DNS；Mac可直接SSH以 `vmcheck` 登录，cloud-init已完成。guest MAC `52:54:00:d3:32:69` 与目标node150的libvirt接口一致，UUID `10cd51f6-d6e5-486b-b07d-0227ff05bee9`。
- finally已删除该domain及专用qcow2、seed ISO共2个文件，跳过0；临时测试地址不再由VM占用。

证据：[迁移](evidence/v1.1-bridge-migration.json)、[最终部署与网络默认值](evidence/v1.1-deployment.json)、[真实guest核对与清理](evidence/v1.1-e2e-bridge.json)、[创建表单](evidence/v1.1-bridge-static-form.png)、[成功任务](evidence/v1.1-bridge-task-success.png)。

### 管理员改密：通过

顶栏“修改密码”提供当前密码、新密码及确认密码。真实浏览器依次验证：

1. 两次新密码不一致时拒绝，仍在对话框。
2. 当前密码错误返回403，不注销已有会话。
3. 成功改为仅在内存保留的临时口令后，当前及另一会话均401，旧口令401；可用新口令重新登录。
4. 再通过同一界面恢复原交付口令，恢复后的登录成功；本次验收未改变用户当前持有的交付口令。

新密码要求12–200字符，成功后全部会话立即失效。口令未进入任务、浏览器持久存储或普通日志。本地 `ADMIN-CREDENTIALS.txt` 不会被浏览器自动修改；管理员以后手动改密须自行安全保管新口令。

证据：[改密实测](evidence/v1.1-e2e-password.json)、[改密入口](evidence/v1.1-password-dialog.png)、[成功后重新登录](evidence/v1.1-password-changed.png)。

## 自动化与回归

- 完整单元：Python3.13.7本机、3.6.8的node30、3.12.3的node150，三端最终均 **83/83通过**，无失败、错误或跳过。含静态参数校验前置、DHCP旧请求兼容、MAC对应、seed真实写入、错误地址／网关／DNS拒绝、改密与持久化、部分seed失败清理及重试。
- JavaScript语法检查通过。最终源码哈希绑定在各单元日志第二轮与部署记录中。
- 隔离表单回归 **3/3通过**：非首项镜像保留、桥接／NAT独立默认源、退出单次绑定。该场景使用显式demo夹具，不代表生产主机数据；真实桥接与改密另有正式入口验收证据。见 [表单结果](evidence/v1.1-e2e-forms.json)，以日志ROUND4为最终通过轮次。
- 旧环境与清理最终核对 **通过**：8088 HTTP200，新服务active/enabled；旧源码／配置／数据库及14台VM的UUID／状态／XML哈希均与原基线一致；node150桥接管理地址／网关／DNS和原NAT保留，0台VM、0个临时文件。见 [最终环境](evidence/v1.1-environment.json)。

日志：[本机](evidence/v1.1-unit-local.log)、[Python3.6](evidence/v1.1-unit-python36.log)、[Python3.12](evidence/v1.1-unit-python312.log)、[JS](evidence/v1.1-js-syntax.log)。

首轮桥接脚本已实际取得正确静态IP／路由／DNS，但因使用 `ip -4 address` 过滤输出缺少MAC字段而失败；修正为完整网卡查询后重跑通过，MAC检查未跳过。旧失败及第二轮成功均保留在同一日志。部分seed失败清理、镜像重置与按钮绑定的定向复核发现已修复并重测，没有重启广泛复审。

## 源码同步与保密

远程仓库：<https://github.com/ioppe/cloudStack.git>，工作分支 `main`。首次提交包含可维护源码、配置示例、部署与网络回退说明、测试和安全验收证据；管理员凭据、运行配置、私钥、数据库、缓存和release归档均被忽略，提交前按实际Git文件清单执行秘密匹配扫描与交付门禁。

软件旧版源码备份位于两机 `/var/lib/vm-platform/backups/platform-code-1.0.tar.gz`；网络原配置备份位于node150 `/var/lib/vm-platform/backups/node150-bridge-v1/`。旧 `.30:8088` 面板、原有14台VM及其业务数据不纳入改动范围。

保密核验：候选Git文件与两机新平台服务日志中，当前管理员口令及私钥标记命中均为0，见 `evidence/v1.1-source-safety.json`。两机 `/opt/vm-platform-v11-check-only` 已删除，guest一次性私钥仅保留在会话scratch，未上传。
