# VM Platform 首版实现约定

运行环境：控制端 192.168.0.30 Python 3.6.8，执行端 .30 Python 3.6、.150 Python 3.12。只用 Python 标准库；新服务路径 /opt/vm-platform，控制端 8089，旧 /opt/vm-panel 和 8088 保留。

## HTTP API（同源、会话 Cookie、写请求 X-CSRF-Token）
所有返回 JSON。错误 {error: 中文消息} 配合 400/401/403/404/405/409/429/502。所有动态 HTML 字符须转义，无 innerHTML 插入不可信字符串。
- POST /api/login {username,password} -> {username,csrf_token}; HttpOnly SameSite=Strict session cookie。
- GET /api/session -> {username,csrf_token}; 未登录 401。
- POST /api/logout {} -> {ok:true}。
- POST /api/password {current_password,password} -> {ok:true}；顶栏“修改密码”入口，12–200 字符，成功清除全部会话并要求重新登录。
- GET /api/hosts -> {hosts:[Host]}，只读登记，不等待远端。
- POST /api/hosts {id,name,address,transport:"ssh",ssh_user:"root",ssh_port:22} -> {host:Host}。
- PUT /api/hosts/{id} 同上（id 不可改）-> {host:Host}；local 主机仅允许改显示名称；local 登记仅由控制端配置首次初始化，API 不接受新增 local。
- DELETE /api/hosts/{id} -> {ok:true}，只删登记，有任务执行时拒绝。
- GET /api/hosts/{id}/status -> {host:Host,online:true,status:Status}，失败 502 {error,...}。
- GET /api/hosts/{id}/vms -> {vms:[VM]}。
- GET /api/hosts/{id}/images -> {images:[Image],dirs:[path],disk_dirs:[{path,free_human,total,used,free}]}。
- GET /api/hosts/{id}/networks -> {bridges:[name],libvirt_networks:[{name,state}],default_bridge,default_network,default_type,default_source,static_network_defaults:{bridge:{cidr,gateway,dns:[IPv4]}}}；切换模式使用独立默认源，只返回实际网桥的有效 profile。
- POST /api/hosts/{id}/vms {kind:"disk"|"iso",name,source,disk_dir,memory_gb,vcpus,disk_gb,network_type:"network"|"bridge",network_source,cloud_init_enabled,ci_username,ci_password,ci_ssh_key,ci_dhcp:true|false,ci_isolated:false|true,ci_ip,ci_gateway,ci_dns} -> 202 {task:Task}。
  ci_dhcp 缺省／true 为 DHCP，忽略静态字段；false 仅接受 disk + cloud-init + bridge，ci_ip 为 IPv4/CIDR（前缀 1–30，可用主机地址）。默认联网需要同子网另一合法网关及非空 IPv4 DNS；缺项仅从同网桥且 guest IP/profile CIDR、profile 网关/guest CIDR 均匹配的静态 profile 补齐，不覆盖手填。仍缺项在写盘前拒绝。ci_isolated:true 不套用默认值、不生成路由或 DNS，同时传非空网关/DNS 拒绝。该接口不重写已有 VM。
- POST /api/hosts/{id}/vms/{uuid}/action {action:"start"|"shutdown"|"stop"|"reboot"|"suspend"|"resume"|"delete",remove_disks:false|true,force:false|true} -> 202 {task:Task}。
- POST /api/hosts/{id}/vms/{uuid}/config {vcpus,memory_gb,disk_gb,resize_guest:false} -> 202 {task:Task}。停机配置，禁止磁盘缩小。
- GET /api/tasks -> {tasks:[Task]}，host_id 可 query 过滤。
- GET /console?host={id}&uuid={uuid} -> 控制台页面（登录必要）。
- WebSocket /api/hosts/{id}/vms/{uuid}/vnc -> binary raw VNC，认证并检查 Origin，先核验 host/UUID。
- GET /health -> {ok:true,version:"1.1.2"}。

Host: {id,name,address,transport:"local"|"ssh",ssh_user,ssh_port}（不包含私钥或密码）。
Status: {hostname,os,cpu_count,memory_total,memory_available,disk_total,disk_free,vm_count,running_count,kvm:true} 字节单位。
VM: 复用旧代码字段，并加 uuid/host_id；{uuid,name,state,cpu,max_memory,interfaces:[{source,mac,ips:[],ip_source}],ip_addresses:[],ip_status,ip_status_reason,disks:[],metrics:{cpu:{percent,text},memory:{percent,text},storage:{}},config:{vcpus,memory_gb,primary_disk_gb,...},has_console}。state 兼容 running,paused,shut off,saved。ip_status 为 resolved/discovering/unavailable/not_running，ip_source 为 agent/dhcp/neighbor/arp 或空；未探测到不证明 guest 无网络。
Image: {name,path,type:"iso"|"disk",size_human,format,virtual_size,metadata:{alias,os_family,os_version,...}}。
Task: {id,host_id,host_name,vm_uuid,vm_name,title,status:"running"|"success"|"failed",created_at,finished_at,error,result}。时间戳秒；result 只保存安全摘要，不保存含密码的命令/创建参数。

## 执行端
worker.py --serve 在 /run/vm-platform/agent.sock 提供 root-only Unix socket，systemd 开机启动。worker.py --rpc stdin 接收一行 JSON {operation,payload} -> stdout 一行 {ok:true,data:...} 或 {ok:false,error:...}，通过 SocketHost 转发给常驻服务，不直接执行操作。worker.py --ssh 是 SSH forced command：仅允许 SSH_ORIGINAL_COMMAND 为 rpc 或 vnc <UUID>。--vnc 经 agent 校验 host/UUID 与回环地址后转发原始字节。所有客户端模式须以 root 访问本机 socket，要求 agent 已运行；离线返回连接错误，不回落到其他主机。配置 VM_PLATFORM_AGENT_CONFIG，默认 /etc/vm-platform/agent.json，允许目录由执行端自己的配置控制。
operation: status, vms, images, networks, create, action, config, vnc_target。action/config/vnc_target payload={uuid,...}，校验 UUID 后用 domname 解析当前名称并核对 domuuid，不接受以名称冒充 UUID。
异常返回安全信息；配置和删除验证磁盘路径在允许磁盘目录，ISO/template 只读；命令固定 argv 不接受任意 shell。所有 VNC 新建 listen=127.0.0.1；NAT 地址结合 guest-agent 与 net-dhcp-leases。
地址发现由执行端本机处理，`bridge_ip_discovery` 默认 false；显式启用后仅运行中、被动来源无地址的 bridge 网卡可加入后台 ARP 查询。网段必须来自真实本地 Linux 网桥 IPv4，仅 /24–/30，每轮最多 253 个请求；单轮不超过 3 秒、每桥最短间隔 30 秒、正缓存 120 秒、阴性缓存 30 秒，统一使用单调时钟。只接受当前 VM MAC 对应的有效响应，同一 MAC 的多个地址去重保留；缓存按 bridge+MAC 隔离，按当前运行网卡剪枝，并按目标代次丢弃移除／换接口后的迟到结果。不修改 guest、XML 或宿主机网络，不扫描端口。列表请求不等待扫描完成。

## 界面
中文品牌“云栈 / 虚拟机平台”；左侧导航 主机、虚拟机、镜像、任务，顶栏显示当前主机及在线状态、刷新、退出。主机页可登记、编辑、检测、移除；虚拟机页有目标主机选择、创建对话框、状态与资源列表、生命周期、配置、删除确认、控制台；镜像和存储真实空态；任务持续轮询显示所属主机/虚拟机及错误。切换主机时清空旧状态并丢弃迟到响应，提交捕获弹窗绑定 host+uuid，忙状态同样隔离。移动端可滚动表格、键盘焦点、对话框可关闭。无 CDN/外部字体依赖。
v1.1.2 在可见且已登录的 VM 列表每 5 秒轻量刷新，单主机请求不重叠，静默刷新不清空列表；切主机／页、隐藏、退出或401停止旧轮询，迟到响应仍丢弃。发现未完成显示“探测中”，无结果显示“未探测到”及原因。
配色：深海蓝 #162c45，主背景 #f2f5f8，正文 #24364a，青蓝 #007f91，在线绿 #188360，警示 #ba543c；中文系统无衬线＋系统等宽数字，留白克制，主机资源用一行容量带、VM 用可扫描表格。不同于宣传页，不做装饰大图或渐变。

## 范围与验收
仅专用临时虚拟机用于写入验收，现有 VM 的运行状态及 XML 在修复前后比对。首版 .30 的 UUID 只读名单已按用户后续授权清空，本轮保留该状态；不开放新 agent/VNC TCP 监听。v1.1 已为 node150 新增物理 br0（eno2np1 为桥端口），管理地址 192.168.0.150/24、网关和 DNS 原值迁到 br0，default NAT 保留且仍为默认。网络变更历史及独立回退见 NETWORKS.md。
