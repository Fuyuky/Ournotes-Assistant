# 协调服务部署与恢复

网站后台 `/admin/` 的私有校验配置、30天登录会话、邮箱反查和四项统计见 [网站后台管理](../../docs/网站后台管理.md)。后台 Cookie 与机器管理员 token 相互独立，不开放注册；将 `/admin/` 明确转发至云端服务，并保持 HTTPS 与私有资源白名单边界。


本目录交付可运行配置，不代表生产服务器、证书、代理或 Gmail 已验收。服务仅提供永久编号、最终私有成果、收码票据及自动公共发布；没有客户端游戏控制接口。部署需要单独的生产授权。

## 安装与权限

将服务端程序、`web/account-browser`、`data/catalog/site` 放入 `/opt/ournotes-cloud`，创建独立 `ournotes-cloud` 用户。在 Linux Python 3.12+ 环境安装项目 `server` extra或依赖组（FastAPI、uvicorn、requests、PyYAML）。`python -m ournotes.cloud --config /etc/ournotes-cloud/config.json` 为唯一服务入口，绑定 `127.0.0.1:8770`，单进程单 worker，并使用OS独占进程锁。不得把 SQLite 放在网络挂载目录。

复制示例配置并填写既有 Gmail 的无点号规范地址。使用系统随机源生成至少 32 字符的管理员 token，写入 `admin_token_file`；管理员 token、Google 私有文件及订阅地址必须仅部署用户/root可读，私有目录 0700、文件 0600，不填入项目模板或客户端包。节点从 `/api/cloud/v1/admin/nodes` 分别建立，收到一次性凭据后保存于对应机器 DPAPI。撤销和轮换不影响其他节点。管理员凭据不当作普通节点凭据使用。

安装服务 unit 后先本机验收；代理服务不加入协调进程的启动依赖。代理停止时 Gmail 返回 503，编号及成果查询继续可用。Nginx 示例只将 HTTPS 转发到 loopback，关闭 access log、限制请求体，公共内容由应用读取已发布指针，私有目录绝不能配置静态 root。

## 迁移与编号保护

新库默认禁止分配。管理员必须核对各节点持久 next_index、全部已分配身份、历史保留区段及本地迁移记录，再调用 `POST /api/cloud/v1/admin/allocations/verify`，正文为 `{test_next_index,formal_next_index,evidence}`。测试区段从 8 开始、小于 1048576，正式区段从 1048576 开始。高水位不能降低；evidence填写核验记录文件和内容哈希，不以成功档案最大编号代替分配高水位。

先为来源机器建立节点，再逐条 `POST /api/cloud/v1/admin/results/import`，正文为 `{request_id,node_id,channel,result}`。旧 account_id从本地运行库身份取证，缺少身份不得伪造补齐；result必须是实际存在的有效成功档案。历史index必须低于已核实高水位，邮箱必须与编号映射一致。重复身份及不同内容冲突拒绝，导入不会分配新编号。服务 schema拒绝UID、owner_use、status及运行字段；完整抽数等于member/support所有R/SR/SSR合计，卡牌明细只含SR/SSR，并通过共享catalog校验。

每次分配在本地 SQLite短事务中写入台账和永久回执，同时在提交前fsync独立 `*.allocation-audit.jsonl`。崩溃窗口内已记入日志的编号仍永久消耗；重启可恢复原请求回执。客户端不确认、不上传或失联都不会回收编号。分号日志和云库放在受限目录，不作为公开资源。

在线一致性备份：`python -m ournotes.cloud --config CONFIG --backup NEW_BACKUP_PATH`。生成的备份默认关闭分配。备份、外置分配日志及安全高水位须另行异地保存；不要简单复制活动库而漏掉WAL。恢复时停止服务、保留当前库与日志副本、替换为备份后启动，保持分配关闭。核对备份之后发出的所有日志/回执及节点领取记录，无法证明安全高水位就保持关闭。核实后调用verify再开放。恢复旧备份可能缺少成果及used更新，需要按版本审计恢复，不能把旧成果用于覆盖新状态。

## IP HTTPS

先确认公网IP控制权、80端口挑战可达及支持IP的Certbot版本。根据 [Let's Encrypt官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)，webroot IP证书需Certbot 5.4+，命令为 `certbot certonly --staging --preferred-profile shortlived --webroot --webroot-path /var/www/ournotes-acme --ip-address PUBLIC_IP`。先只安装80端口挑战段，通过staging后去掉`--staging`申请可信证书，再启用443段；不要用尚未生成证书的完整Nginx配置启动。客户端始终校验证书。

IP证书约六天有效，必须启用Certbot自带renew timer，将 `renewal-hook.sh` 放入受限deploy-hook目录以校验并reload Nginx；安装certificate-check timer，每六小时检查至少36小时有效期。核实证书实际SAN含IP、HTTPS客户端校验成功、renew dry-run及reload生效，并把timer失败纳入服务器告警。模板本身不是实际续期验收。

已有静态站点的首次 HTTPS 接入可在服务器执行 `python3 /opt/ournotes-cloud/deploy/cloud/enable_https.py --ip PUBLIC_IP`。该工具要求正式证书已存在且剩余有效期至少36小时，保留HTTP与静态白名单，仅在新增443入口代理私有API；校验Nginx配置后reload，并安装 `certbot-renew`、`certificate-check` 的service/timer和续期hook。默认Certbot位于 `/opt/ournotes-certbot/bin/certbot`。执行后仍须从客户端验收证书、鉴权、端口可达性与 `certbot renew --dry-run --no-random-sleep-on-renew --run-deploy-hooks`。网站公共根继续指向原发布副本，首次自动发布新云端快照后再切换公共路由。

## Gmail与代理

第一次Google授权由本机系统浏览器完成，Fuyuki使用项目外私有本机工具及服务器维护凭据通过HTTPS `POST /api/cloud/v1/admin/oauth` 将read-only Google credentials迁入服务器。服务器通过显式本机代理刷新令牌、校验目标Gmail后保存，客户端不获得Google token。节点凭据不能调用OAuth接口。需要迁入字段为token、refresh_token、token_uri、client_id、client_secret、scopes、expiry（Google credentials JSON）；仅允许gmail.readonly scope，token_uri只能是官方端点。

Clash订阅地址放入服务器私有文件；显式运行 `update_proxy.py --subscription-file PRIVATE_FILE --output /var/lib/ournotes-proxy/config.yaml`。工具只提取审核支持的proxies节点，忽略订阅顶层监听/规则/管理接口，拒绝跳过TLS校验或未知节点字段，并先运行mihomo配置校验；失败保留原配置，输出不包含地址或节点密码。节点使用本机mixed-port 7890，禁止LAN、没有管理接口；mihomo独立用户、独立service。管理员明确重启代理加载新配置。检查7890未暴露公网，并验证Gmail读取和Google刷新均走该代理。

票据先记录服务端时间、匹配规则版本及已有邮件基线；游戏发送意图由客户端先持久保存。sent通知丢失时查询原票据，也允许prepared票据匹配验证码，不据此重新发码。只匹配原始点号别名、发送者、主题、时间窗口；多个候选返回ambiguous。邮件ID短事务唯一认领，code仅返回票据所属节点，成功结束/到期清除code；定期清理，终态及消息ID保留30天，不存邮件正文。超时或结果不确定禁止自动新建同账号票据。一个票据/节点异常不会暂停其他客户端。

代理组使用 mihomo 的 `url-test`，每五分钟检查 Google HTTPS 连通性并选择可用节点；关闭惰性检查，避免订阅中的余额/时间提示伪节点被按名称默认选中。保持 TLS 校验和仅本机监听，不开放代理管理接口。

## 查询、使用标记与自动发布

单账号上传入口为认证 `POST /api/cloud/v1/results`，请求 `{event_id,version,result}`；原 `PUT /api/cloud/v1/results/{account_id}` 保留同一验证与幂等回执实现。客户端每完成一个完整账号，先持久保存成果及发送队列，再立即上传该账号；失败不影响游戏完成记录，后台重试复用原回执，不要求手动同步，并由服务器自动发布新账号。

新卡上线时必须同步维护服务器 `catalog` 配置指定的卡牌目录：核对 hk-tw-mo 版本，部署 catalog.json、角色映射及新 SSR 图片，校验目录后重启服务加载。客户端能识别新卡不代表服务器能接收；服务器目录缺少 kind:id 会在成果校验时返回422。不得为消除422跳过未知卡或修改已完成账号内容。

所有节点可查询共同成果、修改used。上传仅更新自己的来源集合，独立source_version；used以云端version CAS管理，上传不会覆盖已有used，离线或扫描失败不会删除云端成果。API没有账号物理删除入口。GET results保留兼容；日常客户端通过WSS通知与HTTPS按需查询，不下载完整成果副本。

成果、使用状态及公告变更在数据库持久记录待发布revision；服务器立即尝试发布并后台重试，重启保留目标。共享校验和白名单exporter生成全量有效快照，哈希验证完成后原子切换current；失败保留此前版本，不回滚业务数据。普通客户端没有手动发布入口。

Nginx必须为 `/api/cloud/v1/events` 配置HTTP/1.1 Upgrade、Connection与足够的读取超时，保留Authorization。已有HTTPS站点也需核对这段路由；首次证书安装工具不代表已替换现有TLS配置。服务仍单worker，WSS只发送失效通知而非秘密数据。

## 既有静态站点与云端发布路由

`enable_https.py` 为已有静态站点增加 HTTPS API 时保留旧静态根。首次云端发布之后，还须核对公网 `data/current.json` 是否与 loopback 8770 及发布回执的 `public_revision` 一致；私有发布记录成功不能证明公网已读取新版本。

维护工具 `publication_proxy.py --input LIVE_CONFIG --output NEW_CANDIDATE` 仅为已知的 HTTP/HTTPS 静态站点生成候选配置，不覆盖输入文件、不 reload 服务。候选将首页、index.html 与 current 指针代理到发布服务，保留原公共资源白名单和私有路径404。已有哈希资源继续从旧静态根读取，新哈希回落至云端已发布版本及历史版本；API鉴权、TLS和证书挑战路由保持原样。

生产切换先核验已发布快照的清单SHA-256、账号集合和字段白名单，在包含原主配置的独立测试配置中运行 `nginx -t`。按本次维护授权备份原站点配置，再安装候选、再次校验并 reload Nginx；失败还原原配置。此切换只让已明确发布的快照生效，不调用新的 publications 请求，也不重启协调服务或游戏批次。切换后分别读取公网HTTP/HTTPS指针与快照，核对发布哈希、目标账号及私有路径404。


### 邮箱换绑票据

固定管理员凭据均支持换绑。旧节点继续按 `rebind_nodes` 兼容白名单授权，默认关闭。接口协议和短期秘密处理见 [邮箱换绑指南](../../docs/邮箱换绑.md)。启用前需部署对应服务代码并保留既有完整配置；所有发行客户端包含同一换绑界面。

换绑登录验证码复用服务器 `mail_rule` 的发件人规则，并保留精确收件人、时间窗、唯一候选及 Gmail DKIM 校验；原邮箱认证链接仍使用独立白名单，不能靠扩展登录发件人配置扩大链接确认权限。本机批次与换绑的实例互斥由客户端负责，各自保留所属票据、基线和消息认领；服务器部署不等于已重启本机控制台或验证真实并行发码。

## 管理员与活动迁移

旧admin引导为Fuyuki，历史节点和成果归属Fuyuki但不改原ID。管理员固定凭据加密密钥在数据库旁，必须备份；共享预设与活动库在首次启动从配置/主数据导入。升级前保留一致性备份，先检查新服务与公开资源，再切换客户端凭据。网站只有Gmail健康查询，维护授权接口不得赋给浏览器会话。详见[云端账号与管理员](../../docs/云端账号与管理员.md)。
