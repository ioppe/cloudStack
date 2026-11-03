# v1.1.1 定向复核处理记录

范围仅限本轮静态网络创建修复，执行过一次只读代码复核，未展开其他架构审查。

- 实际问题：从 profile 补入的网关未重新使用 `_static_gateway` 校验。现已在补缺后统一调用 `_static_gateway(gateway, iface)` 与 `_static_dns(dns)`；新增 `test_profile_gateway_is_revalidated_against_guest_before_writes`，分别覆盖网关等于 guest、自 guest CIDR 的广播地址及网络地址，均要求零磁盘／seed 写入。
- profile CIDR 保持规范网络地址的既定契约（如 `192.168.0.0/24`）。带主机位的 `192.168.0.1/24` 属无效配置，不套用；已在 NETWORKS.md 与增量验收文档说明，不放宽为隐式归一化。
- 主会话集成时修正前端初始化顺序：`autoGateway`／`autoDns` 声明移到首次 `syncCiNetwork` 前，避免 TDZ。后续由真实浏览器表单回归覆盖弹窗及自动填充。

三版本最终单元、浏览器与 guest 联网结果分别以 `static-internet-unit-summary.json`、`static-internet-forms.json`、`static-internet-e2e.json` 为准。复核报告本身不等于执行验证。

最终复测：三版本各 97/97 单元通过；真实 guest 9/9、隔离表单 7/7、旧环境核对 9/9 通过。验收脚本修正 MAC 字段来源及复选框工具适配，未删减业务断言，首轮失败证据保留为 round1；详见增量验收记录。
