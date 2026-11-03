# v1.1.1 提交前交付核验

结论：可以提交，阻塞项 0。由 delivery-gate 对同一批已暂存改动只读核验，未重复运行单元／E2E，未改代码或提交。

- `git diff --cached --check` 通过；无冲突标记、实际凭据、私钥、私有配置、数据库、缓存、release 归档或 lockfile 进入本次暂存。
- 最终单元证据为三运行时各 97/97，失败／错误／跳过均为 0；真实 guest 9/9、隔离表单 7/7、旧环境核对 9/9，最终日志退出码均为 0。
- 独立复算 `static-internet-unit-summary.json` 与 `static-internet-final-source.json` 中 17 个文件哈希，均与已暂存内容相同；6 个运行时／配置示例／单元文件的工作区、暂存区与最终同步记录相符。
- 版本 1.1.1、agent 配置字段、networks/create 新契约、前端行为、部署说明、CHANGELOG、spec-sync 台账和增量验收已同步。
- 15 台既有 VM、旧面板三文件哈希与 8088、两宿主机地址／默认路由／DNS、node150 home 挂载依赖及 root-only socket 保持，临时域与专用磁盘／seed 已清理。
- 仓库无独立 spec 矩阵／ADR／i18n／pnpm 工具链，本轮沿用既有文档路径；不增加不存在的检查或产物。

证据阅读说明：`static-internet-deployment.json` 是初次部署时的历史快照，随后更新的文档与验收脚本以 `static-internet-final-source.json` 为最终哈希依据。六个运行时相关文件在两个快照中一致。旧 96 项单元轮次与首轮 E2E 失败均保留为 `round1`，不得作为最终通过证据。

本轮沿既有授权直接在 main 提交，远端提交前仍为基线 `4a9b8f43a7c683cdb12f0591cab40459b0b1df46`。现有 192.168.0.160 guest 未修改，仍需按增量验收文档单独补正 guest Netplan；本次通过结论只覆盖新创建流程。
