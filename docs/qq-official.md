# QQ 官方机器人

本文说明 QQ 官方渠道的配置与运维。OneBot/NapCat 与官方渠道可以分别启用，也可以同时运行；官方渠道默认关闭。两种渠道共享现有模型、任务和投递存储，但各自的账号、会话和权限互不替代。

> 当前真实验收仅覆盖指定测试群的文字、`/help`、小 PDF/ZIP 上传发送与人工下载打开；没有执行生产切换或历史迁移。最新状态见 [实施进度](qq-official-progress.md)。

## 选择平台与腾讯侧条件

| 模式 | `AI_ONEBOT_ENABLED` | `AI_QQ_OFFICIAL_ENABLED` | 启动方式 |
|---|---|---|---|
| 原 OneBot | `true` | `false` | 保留原有 NapCat/NoneBot 流程 |
| 官方单平台 | `false` | `true` | 直接启动应用 HTTP、官方 Gateway 与公共任务 worker；不启动 NapCat |
| 双平台 | `true` | `true` | 两个入口共享应用服务，各自检查账号、端点、来源与权限 |

两者同时关闭是无效配置。官方单平台不需要扫描个人 QQ 登录二维码。更换消息入口不要求重建无关的沙盒镜像。

个人账号是否认证、体验名单、IP 白名单、公开服务和群主授权，分别以腾讯开放平台对当前 AppID 展示的要求为准。不要把某一种账号的条件当作所有账号的统一步骤。`certification=certified` 是部署者记录的认证状态，用于选择本地速率规则，不能取得平台 API 权限。

腾讯允许某人添加或联系机器人，与本地允许其调用模型、创建任务、使用项目、发送文件或操作服务器是不同的决定。启用公开服务不会自动放宽本地入口策略，也不会自动绑定群。

同一 AppID 应有一套明确的活动部署。数据库租约只能排斥使用同一个数据库的实例，无法证明别处没有另一套部署。切换前确认旧实例已停用；测试、生产与 sandbox 的身份分开保存。

## 凭据与最小配置

在 App 引导中选择“QQ 官方机器人”，通过受保护入口保存凭据。控制台只返回配置状态，不返回 AppSecret 或绑定 pepper。手动部署使用以下路径变量，内容不能写入 Git、普通日志或 Nix store：

```dotenv
AI_ONEBOT_ENABLED=false
AI_QQ_OFFICIAL_ENABLED=true
AI_QQ_OFFICIAL_APP_ID_FILE=/run/secrets/qq-app-id
AI_QQ_OFFICIAL_APP_SECRET_FILE=/run/secrets/qq-app-secret
AI_QQ_OFFICIAL_BIND_PEPPER_FILE=/run/secrets/qq-bind-pepper
AI_QQ_OFFICIAL_HOST_CONFIG_FILE=/run/secrets/qq-host.json
AI_QQ_OFFICIAL_PRIVATE_POLICY=owner_only
AI_QQ_OFFICIAL_PRIVATE_ALLOWED_OPENIDS=[]
AI_QQ_OFFICIAL_OWNER_OPENIDS=[]
AI_QQ_OFFICIAL_GROUP_ADMISSION=manual_bind
```

AppID 和 AppSecret 文件各保存一个文本值。pepper 是独立生成的 32 字节安全随机值，以 64 个十六进制字符保存，不能复用 AppSecret。保留 pepper 是恢复原绑定和待处理证据的必要条件；丢失后不能通过随机生成新值来恢复旧数据库的权限。

Unix 使用服务用户或 root 所有、通常 `0600` 的文件。读取器检查实际 ACL；受限的 root/专用服务组读取只有在实际权限验证通过时才接受。Windows 使用原生有效身份、句柄和 DACL 检查，拒绝普通其他用户可访问的文件和不受支持的路径。仅修改 Unix mode 不能替代 ACL 验证。

关闭官方开关时，不读取官方凭据或启动官方运行时。启用后错误配置在连接前失败，诊断不包含密钥或原始凭据路径内容。

App 配置向导生成的 `qq-host.json` 只包含当前规范账号、空 `onebot_accounts` 和按设置选择的 `files`。它不会推导旧 OneBot 来源或生成后台任务策略。需要迁移来源、媒体或后台模型策略时，由可信部署者另行准备受保护的手动宿主配置，字段见 [宿主装配](qq-official-host-config.md)。整个文件是严格校验的 JSON，不接受 Python 导入路径。不要把原始 QQ 群号或普通 QQ 号填成官方 `group_openid`/`user_openid`：这些身份必须来自当前 AppID 的真实事件。

下面是 App/Docker 容器内的最小文件配置，使用容器持久目录 `/data/state`。手动 NixOS 部署时，应把 `private_directory` 改为 `/var/lib/<services.gaoji.stateDirectory>/state/qq-official-files`；服务的 `ProtectSystem=strict` 只为配置的 `StateDirectory` 开放对应写入，不能照抄容器目录。普通本机部署则使用服务用户可写、独占且不对外提供静态文件的私有目录。

`account` 中的 `TEST_APP_ID` 仅为说明，实际值必须由当前 AppID/环境生成；空 `native_routes` 不允许发送原生文件。默认不配置 `download`，因此不会启动下载监听或公网探测。

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP_ID\",\"production\"]",
  "onebot_accounts": [],
  "files": {
    "private_directory": "/data/state/qq-official-files",
    "native_routes": [],
    "generation_enabled": false,
    "max_file_bytes": 20971520
  }
}
```

整个受保护宿主文件最多 16 KiB，未知字段和重复键会被拒绝。布尔值必须为 JSON `true`/`false`；整数不接受布尔值、字符串或小数。以下文件字段都位于同一个 `files` 对象内。S3 不是本次选用的链接后端；不支持的 S3 环境变量不会创建上传路线。

### 文件能力记录

`native_routes` 是必填数组，允许为空、最多 128 条；同一个完整 `endpoint` 与 `file_type` 组合不得重复。每条记录包含：

| 字段 | 类型与约束 |
|---|---|
| `endpoint` | 当前账号、环境下的完整规范 `EndpointKey`，包含实际群或私聊目标；不能填普通 QQ 群号 |
| `endpoint_generation`、`route_epoch`、`capability_revision` | 原验证记录中的正整数版本；运行时必须与当前端点匹配 |
| `max_bytes` | 原验证记录允许的正整数字节数；实际还取文件服务上限和协议上限的最小值，App 导入上限为 20 MiB |
| `verified_at`、`expires_at` | 原验证与到期 Unix 秒整数，`0 <= verified_at < expires_at`；不能用保存时间刷新 |
| `contract` | 原验证契约字符串；当前支持 `qq-autogen-2026-08-10-zero-v1`，旧记录不能改写为当前契约冒充重验 |
| `file_type` | 整数 `1`（PNG/JPEG）或 `4`（PDF/ZIP） |
| `multipart` | 可省略的布尔值，默认 `false`；仅 `file_type: 4` 可为 `true`，必须来自单独的真实分片验收 |

这里不提供带虚构时间、版本或端点的示例证明。空数组可通过配置校验，但不授予任何原生发送能力。

### 分片上传主机策略

省略 `files.multipart` 时不开放分片传输。配置该对象时使用下列字段；主机策略与目标能力记录是两份独立条件，任意一项缺失都不能升级旧小文件路线。

| 字段 | 默认值 | 类型与约束 |
|---|---:|---|
| `allowed_part_hosts` | 必填 | 1–16 个不重复、经明确核验的精确小写 ASCII DNS 主机名；不接受 URL、端口、路径、IP、通配符、localhost 或 `.local`/`.localhost` 名称 |
| `max_parts` | `128` | 严格整数，范围 1–128 |
| `max_session_seconds` | `600` | 严格整数，范围 1–600；原会话期限还取原产物截止与平台响应预算的更小值 |

不得把平台响应中的任意上传地址自动添加为受信主机，也不能在 JSON 中传入传输回调。这里不提供可被误用为已验收主机的示例名单。

### 受控下载后端

省略 `files.download` 时不创建下载监听器或 HTTPS 探测。配置该对象时使用：

| 字段 | 类型与约束 |
|---|---|
| `origin` | 独立的 HTTPS 来源，仅 443（可省略端口）；不能含用户信息、路径、尾斜杠、query 或 fragment；不可使用本地地址，运行时另查 DNS、TLS 与回环 nonce |
| `listen_host` | 必须是 `127.0.0.1` 或 `::1` |
| `listen_port` | 严格整数，范围 1024–65535；不能占用现有服务端口，App 当前另保留控制台端口 8080 |
| `routes` | 必填数组，允许为空、最多 128 条；完整 `endpoint` 不得重复 |

每条 download route 必须包含 `endpoint`、`endpoint_generation`、`route_epoch`、`capability_revision`、`verified_at`、`expires_at`、`contract`，不含 `file_type`、`max_bytes` 或 `multipart`。身份与原时间语义同上，三个版本范围为 1–2⁶³−1，时间范围为 0–2⁶³−1 且 `expires_at > verified_at`；当前可用契约为 `controlled-download-v1`。记录必须来自该实际目标的受控验收，不能由 HTTPS 自检、保存设置或近期发言替代。

下例是可解析的完整宿主文档，使用保留的 `.invalid` 域名和空 routes，仅展示配置形状，**不能作为可用下载部署**。启用此 download 对象会尝试启动回环监听及 HTTPS 自检；测试或正式启用前须配置真实独立反代与证书、替换 origin，并导入真实目标证据。Nix 路径仍按前文替换。

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP_ID\",\"production\"]",
  "onebot_accounts": [],
  "files": {
    "private_directory": "/data/state/qq-official-files",
    "native_routes": [],
    "generation_enabled": false,
    "download": {
      "origin": "https://downloads.example.invalid",
      "listen_host": "127.0.0.1",
      "listen_port": 18081,
      "routes": []
    }
  }
}
```

配置保存与解析成功不代表反代已部署或目标已验收；本次未执行真实反代部署。空 routes 不授权链接发送，即使 HTTPS 自检通过也不例外。

## owner、群绑定与控制台管理员

首次 owner 来自受保护部署配置，不能由第一个访客抢占。先取得并核对当前 AppID 的真实 C2C 身份，再写入 owner 配置。

群绑定按私聊创建挑战、群内提交、原私聊确认的顺序进行。`/bind create` 生成的挑战有期限，只能完成其绑定的流程；群内输入不会直接获得 owner 身份。绑定、端点 generation 和 route epoch 会在后续执行及发送前再次核对。

官方单平台的本地控制台管理员使用 `local_admin` 身份，QQ 数字账号字段为空。可信本机初始化入口可创建第一个本地管理员；`add-local-admin` 在已有管理员的情况下创建额外本地账号。密码经标准输入提交，不能放进命令行参数或 shell 历史。初始化控制台账号不会创造官方私聊身份或群权限。

在已登录控制台中创建管理员绑定挑战，将显示的一次性命令发送到本人真实 owner 私聊，再回到同一控制台会话确认。挑战只存摘要，过期、其他会话、非 owner、群消息或重复提交不能完成绑定。单纯登录控制台、知道 openid 或提供一条近期私聊消息都不足以建立绑定。

需要多人批准的主机操作仍要求多个独立、有效的管理员绑定和原有任务授权。不能把群 owner、控制台密码或“能管理机器人”当作任意群 API、项目或服务器权限。

## 发送、速率与期限

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `AI_QQ_OFFICIAL_MARKDOWN` | `true` | 选择 Markdown 渲染；仅明确未发送的格式拒绝可降级 |
| `AI_QQ_OFFICIAL_MAX_TEXT_CHARS` | `2000` | 每条 Unicode 字符数上限 |
| `AI_QQ_OFFICIAL_MAX_TEXT_PARTS` | `4` | 单次结果最多分成几条，每条分别记账 |
| `AI_QQ_OFFICIAL_MAX_TEXT_BYTES` | 空 | 可选 UTF-8 字节上限，与字符限制同时检查 |
| `AI_QQ_OFFICIAL_DEFER_MAX_HOURS` | `24` | 持久结果的原始等待上限，范围 1–24 小时 |
| `AI_QQ_OFFICIAL_ALLOW_NEWER_WINDOWS` | `true` | 主动发送不可用时，能否借用这个会话里较新消息的回复窗口 |
| `AI_QQ_OFFICIAL_CERTIFICATION` | `unknown` | `unknown / unverified / certified`；未知使用保守速率 |
| `AI_QQ_OFFICIAL_PROJECT_GRANT_TTL_SECONDS` | `86400` | 新项目授权的最长有效秒数，范围 60–86400 |
| `AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS` | `3600` | 新链接的最长有效秒数，范围 60–3600；不改变已有链接 |

`AI_QQ_OFFICIAL_RATE_GROUP_PROACTIVE_PER_MINUTE`、`RATE_C2C_PROACTIVE_PER_SECOND`、`RATE_C2C_PROACTIVE_PER_MINUTE`、`RATE_ENDPOINT_PROACTIVE_PER_MINUTE` 和 `RATE_PASSIVE_PER_SECOND` 可选地进一步收窄本地速率。实际变量均有 `AI_QQ_OFFICIAL_` 前缀。它们不能提高固定协议限额、绕过平台 cooldown 或创造被动回复窗口。

等待中的结果保留原任务、授权、产物及截止时间；重连、新消息和进程重启不会自动续期。过时的主动插话、OTP、已撤权任务不能借用持久结果等待策略恢复。平台响应不明时显示 `unknown` 并保留证据，不立即补发一份可能重复的消息。

项目授权上限同时用于原生创建/命令和控制台新建的迁移授权。显式授权可以更短；已签发授权的期限不会随配置变化自动延长或改写。项目任务继续持有原授权版本、能力与到期时间。近期发言记录用于识别来源，不能替代成员名册或新的授权。

### 任务进度与答复分段

用户后续要求恢复文件/Sub-Agent 的中途 `say`，以及普通最终答复的自然段分隔；本次修复的验证和部署状态见[实施进度](qq-official-progress.md)，不能沿用此前全量结果宣称新行为已上线。

`say` 工具和 Coordinator 的进度回调使用现有 M3 持久发送器。只有原任务仍获授权且存在合法回复窗口或已验证的主动权限时才可发送；没有这些条件就抑制进度，不用它试探主动权限。进度最多 200 字，低优先级，最长等待 30 秒且不超过原任务期限。每个被动窗口最多一条已用或预留进度，同时按最终文本的最大分片数预留额度（默认四片）。入队返回 `enqueued`，不等于平台已接受。原任务取消、撤权、失锁或路由版本改变后不再发送。

普通最终答复在模型完成后一次性冻结，按自然段空行或 `[split]` 分片；代码等受保护内容中的标记不会被当作段落指令。各片依次通过 M3，仍受配置的字符数、字节数和最多分片数限制，逐 token 流式输出保持关闭。Sub-Agent 最终答复优先合并到三个自然段消息，保留原四片硬容量；短段可合入末片，不能丢弃尾部文字。默认五次窗口的一条进度加三条后台结果仍给当前前台回合留一位；确需第四片的长结果仍受后台预留规则，可能等待另一个经许可的窗口或到原截止时间，不宣称同一窗口一定发完。真正超过硬四片容量时保留原冻结正文和期限，不创建残缺计划，并明确报告需要附件、尚未发送；不会自动伪造文件降级。独立文件消息也分别消耗平台额度。

进程重启取消旧的未发送进度，不恢复过时的插话。已经发送而回执不明的进度或正文保持 `unknown`，不会换序号补发；后续正文分片也不能越过未确认的前片。已有最终计划恢复时使用原正文、分片与截止时间，不重新分段，晚到的真实回执仅结算原尝试。

## 文件交付与大小

文件生成、上传和消息发送是三个不同状态。收到上传结果不代表用户已收到文件；仅实际消息回执才能确认投递。回执不明时保留不可变产物与记录，不能把文本说明或文件名当成文件交付。

小文件直传默认上限为 **3,500,000 字节**。PDF/ZIP 的分片路线需要目标证据明确包含 `multipart=true`，同时在 `files.multipart` 配置经过核验的 `allowed_part_hosts`；旧小文件证明不会自动升级。有效大小上限取目标证明原有 `max_bytes`、`files.max_file_bytes`（默认 **20 MiB / 20,971,520 字节**）和本地协议上限三者中的最小值。App 当前也限制最多 20 MiB。PNG/JPEG 保持 3,500,000 字节，不启用分片。

已验证的真实样本是指定测试群的一份 627 字节 PDF 和一份 180 字节 ZIP，用户确认均能下载和打开；这不等于所有端点、格式和尺寸均已验收。超过 4 MB 的三片上传、恢复与 M3 回执仅通过本机 TLS/FakePlatform 测试，真实 QQ 大文件仍待验收。没有该目标的分片证明时，按原小文件路线使用。

文件出口由受保护 host 配置选择：原生路线需匹配账号、精确端点、类型、generation/epoch、能力版本和未过期的验证记录；链接路线还需要独立 HTTPS 下载服务。分片主机配置只能使用 1–16 个精确小写 ASCII DNS 名称，不能填 IP、端口、URL、通配符或本地域名；最多 128 片，原会话有效期最长 600 秒，均可进一步缩短。签名上传地址保存在受保护文件，数据库只保留摘要和固定期限。恢复复用同一会话，已确认的片不重传，结果不明时不新开会话或自动改发链接。保存配置本身不证明路线可用。超出已验证尺寸或没有可用路线时，显示明确的交付失败/不可用状态，不偷偷转发至旧群或公共网盘。

产物先从沙盒导入私有持久存储，核对哈希和大小，再记录任务引用。`files.max_file_bytes` 是持久产物接收上限，不能提高腾讯单次上传上限。空间配额、可用磁盘、账号/用户并发限制独立检查。私有目录不能使用网页静态目录；App 容器的持久 state 挂载位于 `/data/state`。

下载链接最多有效一小时，并且不得晚于原产物实际保留期限。下载服务只提供附件响应、`nosniff` 和禁缓存头，不提供可执行 HTML 预览，也不共享管理控制台 cookie。持有链接的人可能转发它，因此不能承诺只允许原群成员下载；敏感文件需要明确获准的交付方式。

下载后端绑定固定 loopback 地址，并通过同一受信部署中的 HTTPS 反向代理访问。当前 HTTPS nonce 回环检查只证明代理到达这个后端；目标 QQ 场景是否可达仍要有独立的真实验收记录。App 容器内的 loopback 不能通过普通宿主端口映射访问，代理需要位于同一网络命名空间。代理配置与目标验收未通过时，界面应显示未可用。

已签发链接保留原始 publication 与绝对期限；临时连接探测失败不会重新签发链接或延期。下载仍在读取过程中检查原来源、授权、撤权及保留期限。清理会删除到期且无有效引用的实际字节，保留必要的回执和审计记录。

## 媒体与后台能力

图片、语音转写、附件正文、链接与转发内容均是外部数据，不能提升工具或身份权限。附件下载执行 SSRF、大小、时间和内容检查。腾讯未提供 ASR 文本时不能假装已经听过；结构化合并转发保留作者与递归证据，出站采用有限文本或文件。

官方渠道没有验证的群文件区、完整成员列表、贴表情、群名片等工具不会提供给模型。“近期发言者”只表示账本里的近期来源，不表示完整或实时群成员。

提醒、例行任务、主动聊天、wiki 与项目任务继续使用现有持久队列。开启这些能力需要受保护的模型 profile、成本/次数策略、执行期限和来源授权；仅打开 Gateway 不会自动开启后台模型消费。当前项目运行时组合要求同时启用其子任务基础服务，启动配置会明确拒绝不完整组合。

## NixOS 与备份恢复

NixOS 使用 `services.gaoji.oneBot.enable` 和 `services.gaoji.qqOfficial.enable` 选择平台。官方配置中的 `appIdFile`、`appSecretFile`、`bindPepperFile`、`hostConfigFile` 必须是 Nix store 外的绝对字符串；不要使用 Nix path literal、`builtins.readFile` 或内联秘密。模块通过 systemd `LoadCredential` 传递文件。

渲染、等待、速率及项目授权选项与上表一一对应，使用 camelCase 名称，例如 `maxTextChars`、`deferMaxHours`、`projectGrantTtlSeconds` 和 `fileLinkTtlSeconds`。身份列表作为普通 Nix 配置会进入 store；需要隐藏身份时应使用已有的受保护环境文件入口。

Nix 求值通过只证明模块配置可生成，不能替代目标系统上的实际文件系统/ACL 验证、网络连通与文件下载验收。部署时运行程序仍会重新检查其实际读取的 credential 文件。

备份必须同时包含数据库、私有状态/产物及同一原账号的受保护凭据 bundle，尤其是原 pepper。App 恢复创建新的本地凭据引用，保留原账号与 pepper，并保持机器人停用。缺少 bundle 的旧官方备份进入 `recovery_required`，不能填随机新凭据后自动激活旧待处理任务。OneBot 原有备份流程保持兼容。

## 迁移、回退与排障

先验证官方新会话的文字和文件，再用脱敏数据/指定测试入口试验历史迁移。控制台预览必须固定来源、目标、身份映射、项目授权、任务和产物原期限，经过明确审阅后才能冻结并切换。切换不会把旧 OneBot 数字身份视为官方 openid，也不会把历史发言者变成当前成员。

暂停或回退必须按迁移状态机执行，不能只关官方开关并重发到旧群。旧任务、已发送/结果不明消息、绑定版本和原文件期限需要保留。不能通过降库、删除审计表或重新生成 pepper 来“清空”迁移状态。生产迁移与测试账号启用是两个独立动作。

| 现象 | 检查与处理 |
|---|---|
| 未连接 | 检查启用模式、受保护凭据、当前 AppID/环境及腾讯展示的服务条件 |
| 租约不可用 | 排查同数据库实例；同时核对其他数据库或机器上的同 AppID 部署 |
| Gateway 重连或消息缺口 | 查看恢复点和缺口记录；不要把新 READY 当成旧消息已重放完毕 |
| 已收到消息但未执行 | 检查 owner/群绑定、入口策略、成本额度、来源/端点版本及队列状态 |
| 已执行但未发送 | 查看等待原因、真实回复窗口、限流、原截止时间及各部分回执 |
| `unknown` | 保留原记录和产物，核对实际结果；不要直接制造另一份发送 |
| 文件不可用 | 分别检查原生目标证据、大小、私有存储、HTTPS 回环和实际目标验收 |
| 授权过期或撤销 | 需要新的明确授权；重连、重试和历史记录不能恢复旧权限 |
| 恢复后禁止激活 | 核对原账号、原 pepper 与备份 bundle，不混用目标机器现有凭据 |

控制台状态与现有 SSE 增量更新共用登录权限；公共健康响应只给健康状态，不泄露账号或端点。Prometheus 标签使用有界枚举，下载 URL、openid、消息 ID 和原始错误正文不进入指标标签。

受控验收应保留精确版本、启动模式、目标、文件哈希、发送回执、人工下载结果和清理记录。尚未返回结果的步骤标为待验收；本地模拟、平台接受请求与真人实际收到文件分别记录。完整范围及剩余项见 [接入方案](qq-official-plan.md) 和 [实施进度](qq-official-progress.md)。


### 重新安排尚未冻结的迁移

历史迁移计划到期后，普通官方群聊仍可继续，旧历史尚未开放。管理员准备继续迁移时，可通过现有 `POST /bot-admin/api/v1/qq-official/migrations/{id}/actions` 发送 `action: "refresh"`、当前 `preview_digest` 和新的 Unix 秒 `deadline`（当前时间后、最多 24 小时）。省略 `deadline` 仍保留原期限，到期则拒绝。

这只适用于从未冻结的 `planned`、epoch 0 计划。刷新会重新核对源记录、身份映射和当前服务状态，生成新的审阅摘要，并保留旧预览；随后重新生成公共历史预览、核对摘要，再执行 freeze。消息回复窗口、任务、文件和身份映射的期限不会因此延长。冻结之后仍只能走原验证或回退流程，不能用 refresh 解除冻结。
