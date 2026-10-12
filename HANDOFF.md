# OurNotes Assistant 接手指南

先检查 `git status --short`，保留现场改动，再读 [AGENTS](AGENTS.md)。本文件只记最终实现与入口，不记批次流水或临时授权。

## 当前入口

| 功能 | 入口 |
|---|---|
| 本机控制台 | `scripts/webui.py`、`启动控制台.cmd`；127.0.0.1:8766 |
| 机器接口 | `ournotes/control/machine.py`，同一控制台 `/agent`；薄 CLI `devtools/cli.py` |
| 共用应用服务 | `ournotes/control/launcher.py`、`operations.py`、`query.py` |
| 本机统一状态流 | `control/events.py`、`web/launcher/events.js`；远程通知 `integrations/cloud_events.py` |
| 调度与恢复 | `runtime/scheduler.py` 唯一运行入口；runner及各flow为内部组件 |
| 暂存与云端确认 | `integrations/cloud_sync.py`、`runtime/cloud_storage.py` |
| 在线账号查询 | `control/account_query.py`；不扫描或下载本地永久档案 |
| 共享预设 | `cloud/presets.py`、`control/presets.py`、`runtime/presets.py` |
| 服务器与后台 | `python -m ournotes.cloud --config CONFIG`；`cloud/admin_portal.py`、`web/admin` |
| 管理员与固定凭据 | `cloud/administrators.py`；超级管理员 Fuyuki |
| 活动卡池 | `cloud/activities.py`、`accounts/quality.py` |
| 手动邮件 | `cloud/manual_mail.py`；与自动票据分开 |
| 共用换绑 | `ournotes/rebind`，设备互斥在 `common/devices.py` |
| 共用代理 | `ournotes/proxy`，平台传输 `platforms/proxy.py` |
| 素材 | `catalogs/runtime.py`、`catalogs/masterdata.py`、`catalogs/site.py`；`data/catalog` |
| 更新与构建 | `updates/`、`scripts/build_release.py`、`deploy/client` |
| 便携启动检查与 MuMu 路径 | `deploy/client/boot_client.py` 等待就绪并提示失败；`platforms/mumu_config.py` 检测安装、保存至数据根 `config/mumu.json` |
| 历史数据核对迁移 | `python -m ournotes.maintenance.cloud_storage`，默认只读；显式 `--apply` |
| 离线诊断与证据复核 | `devtools`，不进入发行包 |
| 本机私有辅助目录 | `private-workspace/gmail-authorization` 为独立授权工具，`private-workspace/public-source` 为独立公开 checkout；整体忽略，不进入发行包 |

## 关键边界

服务器保存永久完整成果，本机只保留待上传数据、未决交易和恢复所需信息。上传回执核对内容后持久进入确认阶段，再清理账号行、明细、临时JSON和证据；清理失败重试，不再重新上传。批次保留 `cloud_confirmed` 与 `cloud_totals` 聚合；旧worker的run写入不能覆盖这两项。删除已确认本机账号不能释放共享批次额度。

存在待上传成果时，只阻止领取新账号；已有账号按正常保护流程收尾。分配只由云端完成，永久 request_id/event_id 和来源节点不改写，失败、放弃或失联不回收编号。启动控制台不运行StorageWorker、不整理存储。

本机无网站发布、商品上架、跨机成果下载、独立卡牌筛选或Gmail首次授权入口。服务器成果上传触发持久自动发布，公开站点仍保持白名单、内容哈希和原子版本切换。未完成、失败和放弃账号不进入任何业务账号集合。

Fuyuki可增删管理员，无注册。所有管理员均可操作全部完整账号并按上传人筛选。凭据在后台可查，保持至本人重置或删除；重置使旧客户端连接失效。历史账号归属Fuyuki，保留旧node和回执；原owner可以用新凭据处理旧分配/票据。

共享预设保存/删除使用版本CAS，启动批次固定版本和计划。活动池由后台手动选择，分号时固定活动快照，UP按kind:id去重计数，分母来源主数据；旧明确活动改为一期，其余不补标签。无历史预设不猜补。

手动邮件只面向完整账号，默认十分钟、可选二十四小时，每种用途只显示最新邮件和北京时间收件时间。服务器验证最新十分钟链接后执行，独立幂等回执；读取不执行，不消费自动收码票据。手工与自动不互查，标记已使用独立。Gmail授权只由项目外私有本机工具配合服务器维护凭据操作，网站只看健康。

统一发行无业务variant，平台分Windows x64和macOS Apple Silicon。GitHub Actions 分平台构建；每个平台一个完整 ZIP，初装与更新共用，签名清单内置，Release 不另发更新或校验附件。公开仓库Fuyuky/Ournotes-Assistant匿名检查稳定Release，固定公钥验证签名及平台，不自动安装。Mac适配代码和构建不等于Mac MuMu实机执行验收。

## 恢复和维护

状态核验读取 `state/runner-status.json`，以SQLite只读连接核对批次、runs/accounts/draws，另查进程与锁。状态文件可能陈旧，调度complete不代表完成目标或上传已确认。完整成果计数与处理数量分开。

提交抽卡后无证据不能重发。已有明确放弃策略保留cancelled、submission_abandoned与原交易证据，不回收编号，不增加完成数。已有暂停、邮件限额、未知发信和换绑保护不因升级解除。

固定素材移至 `data/catalog/snapshots`，旧路径兼容解析不改哈希。四个历史日期回归目录改为 `async-pipeline`、`gacha-workflow`、`pool-survey`、`game-onboarding`。不能按日期或空壳外观删除仍被账号引用的素材。

历史档案迁移必须核对服务器，缺失、冲突及未确认状态保留。清理被安全审查拒绝时不得绕过；使用项目维护规则生成固定边界脚本交由用户手动处理。未执行不能报告已释放空间。

## 文档

[云端账号与管理员](docs/云端账号与管理员.md) · [架构](docs/架构与部署.md) · [控制台](docs/WebUI启动控制台.md) · [发行](docs/客户端构建与更新.md) · [运行恢复](docs/运行与恢复.md) · [迁移维护](docs/迁移与维护.md) · [测试规范](docs/测试报告与巡检规范.md)
