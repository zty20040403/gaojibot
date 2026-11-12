# gaojibot 改造交接文件

## 开发记录（2026-11-12）· Python F4 TLS 实连回归（未部署）

- 新增本地真实 TLS 回归：测试连接器仅把已经通过公网检查的数字地址映射到回环服务器，生产没有该映射；服务器实际收到原域名 SNI/Host，客户端仍校验证书。错误域名证书被拒绝，HTTP 认证头尚未发送。
- 两项 TLS 测试与 Ruff 通过；只使用临时自签名测试证书和 TEST 凭据，没有对外连接。随后开始固定快照全套复测，结果另记。

## 开发记录（2026-11-12）· Python F4③ GitHub 身份与技能（未部署）

- 宿主配置支持公开 `github.login/user_id`，会话 VM 创建/启动及后台接管已运行 VM 时，通过 guest agent 设置 `/etc/gitconfig` 的 name/noreply 邮箱。只传公开身份，失败明确报错；模板/非会话 VM 不写入，token 仍只在宿主 API 的私有文件里。
- 配置文档提供 `github` REST 与 `github-git` Basic 两个 API 的测试占位示例；补齐 `skills/github.md`、后台规则和双语 README。明确用户要求后才开 PR、AI 英文署名、禁止 VM 登录/缓存 GitHub 密钥、未知写入结果先查询而非重试。
- 28 项 GitHub/VM 替身/任务代理组合通过，含真实临时 git 配置写入，以及真实 PostgreSQL 的 `/stop` 后 `/start` 仍拒绝旧任务 API、持久任务取消立即拒绝；Ruff/两个导入检查通过。没有真实 GitHub 请求、真实 VM 或 PR，不能冒充外部验收。
- F4 的任务令牌路线、审计管理台和 GitHub 本地代码已齐；VM 令牌按依赖待 F2。固定快照完整复测待后续记录。

## 开发记录（2026-11-12）· Python F4② 管理台审计（未部署）

- 管理台“任务与投递”加入宿主 API 审计，可按 API 名/物理 scope 精确查询，按游标向前翻页，并显示所选范围 24 小时结果计数。只读 `/api/host-apis` 同时有 v1 别名；未登录和普通成员不能读取。
- `pending` 显示“尚未确认结束”，流中断显示“结果待核对”，不把未知结果当成未执行或免费；页面没有自动重试按钮。接口不返回 credential 配置、query 或 body。
- 11 项真实数据库/登录/路由边界测试通过，UI TypeScript/Vite 构建通过，Ruff 通过。尚未部署的 `0081` 同步增加时间索引。GitHub 配置/提交身份继续实施，VM 令牌仍待 F2。

## 开发记录（2026-11-12）· Python F4① 任务入口与撤销（未部署）

- `/api/<名字>/<路径>` 接到现有 Codex 中转，仅接受绑定了原始任务来源和实时权限检查的任务令牌。会话从宿主保存的物理 endpoint 推导，不信任请求里的 scope 或历史别名；后台提示词只带 API 名、地址、规则、额度和 about，没有密钥路径/header/query。
- 请求体流式限量、并发有界；排队后、读完请求后、DNS 后、收到响应后和响应块之间复查权限。撤销持有整个 ASGI 生命周期，关闭在途上游连接并落审计；任务结束不留下可用令牌。
- 主动 RED 回归发现把二进制中断当作 SSE 正常结束会令客户端误以为下载成功，已改为异常断开连接；已知 HTTP 状态仍保留，审计记 interrupted，不退额度、不重发。
- 38 项配置/传输/真实回环 HTTP/真实 PostgreSQL/Codex 组合通过，另 14 项代理/官方后台/视频组合通过（有重叠，不相加）；Ruff 和两项导入检查通过。管理台、GitHub 及 F2 VM 令牌仍待接入。无生产访问，测试上游为替身。

## 开发记录（2026-11-12）· Python F4② 持久额度与审计基础（接线中）

- 迁移 `0081_host_api_calls` 新增 `host_api_quota` 和 `host_api_calls`；按物理会话/API/整点小时原子预留，失败和未知结果仍消耗预留，不自动重试有副作用的请求。审计只保存方法、规范路径、来源任务、时间和结果，不保存 query/body。
- 4 项真实 PostgreSQL 回归通过：八个并发请求恰好三个获准；新建 store 不清额度；会话/API/小时互相隔离；审计失败连同预留回滚；终态只写一次。数据库基础已就绪，实际路由与管理台仍在接线。

## 开发记录（2026-11-12）· Python F4① 固定目标传输（接线中）

- 新增 `host_api_transport.py`：实际连接使用已验证公网 IP，保留原 Host/SNI/TLS 校验，拒绝混入私网的 DNS 结果及常见 IPv6 过渡地址；没有重定向、环境代理或共享 cookie jar。复用的是 HTTP 钉住实现，不恢复 MCP。
- 上限 64 MiB，响应头总等待最多 60 秒；宿主覆盖认证头和固定查询字段，删除调用者认证/cookie、hop-by-hop 和方法覆盖头。错误只返回固定诊断，不含带密钥 URL。
- 正常响应逐字节流式返回；阻止响应头及跨块正文直接反射凭据（含常见 URL/JSON 编码），不向 VM 转交 Set-Cookie 或不透明的 Content-Encoding。上游仍必须是 owner 信任的服务，不能把通用代理当作内容级授权。
- 声明与传输共 13 项测试通过，使用注入的传输/DNS 替身，没有外网请求。真实入口、任务身份、额度/审计和管理台仍待接线，不宣称 API 已可用。

## 开发记录（2026-11-12）· Python F4① API 声明与路径规则（接线中）

- 已读 maxx 设计的 Host-keyed APIs / GitHub 部分。新增 `host_apis.py`：默认关闭的命名 API 声明、HTTPS 固定基址、私有密钥文件读取、header/query 占位符、方法/路径段前缀规则和额度字段；宿主配置接收 `apis`。尚未挂转发路由，不宣称 F4 已可用。
- 路径拒绝点段、编码斜杠/反斜杠/点/百分号、控制字符及非规范双斜杠。配置不读取密钥，错误消息和 repr 不暴露受保护值。一个 API 声明不合法则该组代理配置拒绝启用，不影响已合法的媒体能力。
- 10 项声明/路径/密钥文件/宿主配置测试通过，Ruff 和 diff 检查通过。后续继续代理传输、任务身份、按会话额度/审计/管理台，再接 GitHub 与 VM 令牌；API 密钥及 GitHub 账号仍由 owner 提供测试配置，不访问生产。

## 开发记录（2026-11-12）· Python 阶段 A 完整复测（未部署）

- 固定 `5852d55` 快照全套 **1,480 项、0 失败、10 跳过**，耗时 572.101 秒，日志 `/tmp/gaoji-python-stage-a-final.log`。已包含 `9801026` 批次/重启修复和 `5852d55` 中转/RPC 清理；之前 `53e389b` 的一项失败不再遗留。
- F1 与 F9① 的 Python 本地实现及回归完成；UI 构建、Ruff、两个导入检查均通过。源码仍在隔离的 `bot-python-next/`，没有部署，也没有用本地替身冒充真实 QQ/VM/模型验收。
- 接着按阶段 B 的 F4 → F2 → F3 推进。F2/F3 新入口必须复用 F9 的物理 endpoint 停止状态、持久 generation 和出站检查；程序读操作不因停止被关掉，发言/唤醒/发文件要拒绝。

## 开发记录（2026-11-12）· Python F9① 模型中转与 RPC 取消清理（未部署）

- RED 回归确认旧 Codex 中转仅拒绝撤销后的新请求，已经等待响应头或读取响应流的模型调用仍继续。现在按任务令牌登记完整 ASGI 请求，撤销即取消在途请求并关闭上游连接；响应尚未开始时返回 401，已开始的流不伪造 completion。尚未被引擎接管的 TCP 隧道也会关闭。
- 修复 Codex 中断 RPC 的 shield 超时泄漏：有界中断超时后取消原 RPC，收尾等待 reader 和宿主工具子任务退出，再交回任务状态。模拟不回应 interrupt 的 app-server 测试证明不遗留 RPC 或工具任务。
- 14 项 Codex 引擎/真实回环 HTTP/TCP 中转测试通过，Ruff、两个导入检查通过。仍不等于真实模型已退款或 VM 的所有进程已停止；费用未知部分不能按零处理，真实 VM/平台验收保留在任务 9。

## 开发记录（2026-11-12）· Python F9① 批次与重启竞态复查（未部署）

- `53e389b` 固定快照全套 1,470 项完成：1 项失败、10 跳过。失败是旧回归期待正常取消后直到重启才把日志记为 crashed；现在取消当场保留已知用量并终结为 aborted。已补明确断言：正常取消保留 aborted；异常执行失败仍为 crashed，不把真正错误混作取消。
- 主动复查又用 RED 回归确认：周期状态核对误清当前代的连续消息计数；另一进程停止/恢复时新消息可能混入尚未清掉的旧批次；重启恢复试图给旧代回合补发通知，触发送出门槛并阻止启动。批次键现包含 stop generation，只清旧代；恢复先核对原消息代数，不为旧回合入队结果或重启通知。
- 新增恢复前/恢复后重启、延迟控制通知与正常 burst 的回归；这些与日志/中转清理合并的 30 项针对性测试通过。完整复测须以修复后的新快照为准，旧 1,470 项不能记为全绿。

## 开发记录（2026-11-12）· Python F9① 管理台会话控制（未部署）

- 管理台「模型与群友」增加物理群聊/私聊的停止、恢复和取消后台任务入口；登录管理员点击即授权，保留 CSRF/Origin，不新增 QQ OTP。数据库状态、If-Match 版本及管理审计同一事务提交，提交后才通知运行期；同版本并发请求仅一个成功。
- 26 项管理台、认证边界、入站和在途组合回归通过，含另一进程改数据库后取消模型、失败回滚、缺失版本、伪造字段及未知端点拒绝。TypeScript/Vite 构建、Ruff、两个导入检查通过；完整固定快照回归继续跑。管理台已知依赖审计告警仍按单独依赖修复记录处理，没有运行强制升级。
- 双语 README 与功能方案已同步。F9① 本地链路齐全；F9②–④、F2/F3 的新程序/命令入口还未实施。未部署、未连接生产；真实 QQ 角色/回执、VM 进程停止仍由 owner 测试环境验收，不能把本地替身结果当作真实验收。

## 开发记录（2026-11-12）· Python F9① 在途取消与停止回执（未部署）

- 会话控制命令绕过长回合的端点锁，入站提交后取消该端点旧代的模型/主动分类/后台任务/结果整理，并忘掉未完成消息批次；运行期同时检查数据库，覆盖另一管理进程发起的取消。取消仅针对原任务编号或旧代，不误杀恢复后新任务。
- 回环 FakePlatform 验证：模型挂起时 `/stop` 仍立即回执并取消；停止消息照常入库，`/help` 可用，`/start` 只处理新输入；未完批次不会恢复重放。23 项在途/生命周期/后台相关回归通过，Ruff 通过。
- 中断收尾仍核对账号租约与原回合身份，原子终结日志并仅记一次已知 token；未完成调用在 trace 标 `usage_incomplete`，不声称未知费用是零。后台用量关联现有 `background_job_id`；取消后不为保存报告再启动 VM。管理台入口和完整复测仍待完成。

## 开发记录（2026-11-12）· Python F9① 停止代数与发送边界（未部署）

- 尚未发布的 `0080` 补齐后台任务和文件的代数快照；新回合、主动分类、后台工具、文件上传/发送以及待发计划都复查会话状态。恢复不会释放旧回合、旧任务或旧文件。后台任务开始与结束采用条件更新，取消不能被晚到的成功覆盖。
- 定时提醒/例行任务跳过停止期间的到期执行并记原因，不补发；重复周期保留，恢复后的新周期使用新代数。停止会取消待整理的后台结果通知。
- 发送认领后、发 HTTP 前再次检查；已在途且被平台接受的请求仍结算真实回执，不能假称撤回。47 项既有相关回归、29 项新增及关联回归通过，Ruff 通过；使用真实 PostgreSQL 和回环 FakePlatform，不是 QQ 真实验收。在途任务即时取消和管理台入口继续接线。

## 开发记录（2026-11-12）· Python F9① 控制入站基础（未部署）

- 迁移 `0080_conversation_control` 扩展会话设置：停止状态与代数按物理 `endpoint.scope.key` 保存，不跟随共享历史别名。认证入站事务原子写控制回执、状态、后台任务取消和审计；重复处理不重放效果。
- `/stop`、`/start`、`/kill` 从个人 `/cancel` 别名拆开。只承认顶层认证作者的群主/管理员角色、当前宿主 owner 授权；已获准进入的私聊对方可控制自己的私聊。转发角色、昵称和正文不能授权，超过五分钟的控制命令拒绝。
- 43 项真实 PostgreSQL/命令回归通过，含事务回滚、重放、普通成员拒绝、私聊隔离、后台任务批量取消与跨端点拒绝。Ruff 通过。本小步仅完成入站状态基础；在途回合、主动接话、待发消息和管理台仍在接线，不能当作完整停止能力验收通过。

## 开发记录（2026-11-12）· Python F1④ 后台视频与历史说明（未部署）

- 后台专用 `analyze_video` 读取 VM 已准备的 MP4，用宿主模型调用、日额度和保守费用预留；`record_video_summary` 保存抽帧/转写结论。导入工具返回原媒体句柄，后台提示词要求把路径和句柄一起传递。没有配置视频 profile 时只提供抽帧路线，不新增前台专用工具。
- 迁移 `0079_video_notes`：说明关联原消息、媒体段及后台任务，投影到 `messages_with_captions` 并刷新历史搜索缓存，原始 IR 不改；`usage_events.background_job_id` 关联视频费用到任务。未知费用保留预留，不退款；取消中已完成的调用照常记账但不写说明。任务状态/租约/期限、账号/端点、消息可见窗口在提交时重查。
- 112 项后台/媒体/升级回归、另 37 项模型/协议回归通过；后者含真实 OpenAI SDK 经替身 HTTP 保留 `video_url`。Ruff、两项导入检查通过。FFmpeg 用合成片段，VM 和模型为测试替身；真实 QQ、VM 安装/模型内容理解仍须 owner 验收，未部署。F1 本地实现已齐；固定 `b87df51` 快照的全套复测共 1,449 项，0 失败、10 跳过（606.948 秒）。③旧库升级错误也包含在此次通过结果中。

## 开发记录（2026-11-12）· Python F1④ 视频输入基础（未部署）

- VM 工具新增 `gaoji-video FILE`：仅处理 /work 下文件，先检查 10 分钟上限，再压成 1fps、长边 640、单声道 MP4，上限 32 MiB；源码随 cloud-init/显式 upgrade 安装，本次未升级任何 VM。
- 宿主 `video_format.py` 只做有界 MP4 box/时长元数据检查，不运行视频解码器。模型能力新增显式 `capabilities.video`，仅允许 openai-chat 的 `video_url` 约定，其他协议/文字模型拒绝，回退也必须匹配能力。`AI_VIDEO_PROFILE` 默认空；后台调用和历史结论接入仍在下一小步。
- 35 项本地回归通过，含真实 ffmpeg 生成/压缩的合成音视频、轨道时长与容器拒绝、模型能力、预算；base64 媒体传输字节不再冒充文字 token，仍按配置的完整输入窗口保守预留。未使用真实 QQ/模型/VM，不声称视频功能已完成。

## 开发记录（2026-11-12）· Python F1③ 管理台与小步验收（未部署）

- 管理台“媒体审核 → 图片说明”可按会话开关说明、设置每日 0–10,000 张（默认关闭/200 张），查看今日已排队及累计失败/超额。GET `/api/media` 返回状态，PUT `/api/media/captions/settings` 修改配置；沿用现有登录管理员点击授权、CSRF/Origin 校验、版本及审计，不恢复旧的全局 QQ OTP。
- 启用依赖宿主 `AI_MEDIA_ENABLED=true` 和支持视觉的 `AI_VISION_PROFILE`。开关只影响新入站图片；不回补旧图。关闭会让待处理/在途结果的权限复查失败，已经发生的费用仍保留。原始图和说明留在该会话的可见历史范围，普通图不会自动进可发表情库。
- F1③（入站 `777d111`、处理/投影 `a7408a2`、管理台本小步）已通过 132 项针对性回归，UI `tsc --noEmit && vite build`、Ruff、导入检查通过。F1② 的全套 1,426 项通过、跳过 10 项；③全套 1,438 项发现 1 个旧库升级错误（跳过 10 项）：升级前的入站不应依赖新投影视图；已恢复入站读取原始账本，13 项真实升级与说明回归通过，尚未再次取得全套通过结果。F1 视频部分和后续功能尚未完成；真实 QQ/模型验收、部署仍未进行。
- `npm ci` 的现有依赖审计有 2 个 high（Vite 8.2.2、source-map-js），未混入强制升级；单独依赖修复待办记录了 advisory 和 Nix hash 更新要求。

## 开发记录（2026-11-12）· Python F1③ 后台说明与历史投影（未部署）

- 在原 `media_jobs` worker 中注册官方说明处理器，复用 PinnedContentReader 和 VisionWorker。每一步复查来源会话、端点/账号状态、可见窗口、说明开关及任务租约；最多两次尝试、单次两分钟，不占住数据库事务等待模型。认领数量不超过实际 worker 并发数，避免尚未开始的工作空耗租约。
- 图片说明写 `media_analysis`，在历史投影中以不可信文字显示，并更新可重建搜索缓存，`body_json` 不改。普通图不加入可发表情库；旧图片清理也不删除新官方说明的原图。
- 模型请求先保守预留 `image_caption_bound` 用量，已知用量结算为 `image_caption`，未知调用不退款；除图片日额度外还检查原会话 token/调用额度。相同内容可复用已有说明，重建 worker 后任务与费用仍在 PostgreSQL。
- 76 项本地回归通过，含真实数据库、ffmpeg、固定 DNS 的替身 HTTP、模型网关替身；覆盖历史查找、原始 IR 不变、不自动加入表情库、缓存复用、调用中撤销、未知费用、配额、私网拒绝、过期认领拒绝。Ruff、导入检查通过。管理台开关还未开放；完整 F1③ 仍在实施，真实 QQ/模型未验收。

## 开发记录（2026-11-12）· Python F1③ 图片说明入站基础（未部署）

- 迁移 `0078_image_captions`：默认关闭的会话图片说明开关、默认每日 200 张（Asia/Shanghai）、持久额度和媒体说明投影视图；为官方 canonical 消息建立精确媒体唯一键，避免同 canonical scope 下不同 endpoint 的原生消息编号碰撞。
- 官方入站事务现在写 `message_media`（普通图和表情）；启用后在同一事务预留额度并写 `media_jobs`，重复入站不重复记账、回滚不留下半份任务。未启用和超额分别显式记录 disabled/quota，不复制签名 URL。
- 本小步只提供入站基础，不开放管理台开关，旧媒体 worker 不认领尚未接好处理器的官方说明任务。后台处理器、说明显示和管理台仍在实施，不能将本小步当作说明能力已完成。
- 43 项真实 PostgreSQL 回归通过，含并发额度、重放、日切、事务回滚、旧表情入库；Ruff 通过。未部署或连接生产。

## 开发记录（2026-11-12）· Python F1② 前台图片输入（未部署）

- `media.ocr` 开启且前台 profile 支持视觉时，当前、合并和引用消息的最多四张图片进入最后一条易变消息，不改 system/历史前缀；超额保留标记。`view_image` 返回 `ToolImage`，非视觉模型仍用描述回退；无工具模型也能接收初始图片。
- 用 PinnedContentReader 按实际 DNS/源权限下载，不把签名 URL 传给模型；每次命中回合内缓存仍检查权限。复用 VisionWorker 图像准备入口，以严格模式真实解码、长边 1568、40M 像素解码上限；坏图、超限、缺 ffmpeg、解码超时明确返回失败，不偷偷送原图。图片块标记为不可信资料。
- Nix 包、开发 shell、服务路径和 CI 加入 ffmpeg；宿主配置说明与双语 README 已更新。未做 Nix 完整构建，只验证表达式语法。
- 100 项针对性回归通过，使用真实 PostgreSQL、ffmpeg/ffprobe、FakePlatform 和模型替身，覆盖压缩后小体积大尺寸图、引用图、四图上限、文字模型、跨会话/权限撤销、工具返回图、无工具模型与错误。Ruff、两项导入检查通过。
- F1① 基线全套 1,417 项通过，跳过 10 项；②全套 1,426 项通过、跳过 10 项。真实模型/QQ 验收仍未做；图片说明、视频和 F2–F10 尚未完成，没有部署或合并。

## 开发记录（2026-11-12）· Python F1① 媒体标记与图片查找（未部署）

- 独立 `feat/python-next-features` 分支，基线 `4f2b966`；不改 Java worktree。顺序与剩余要求见 `docs/next-features-plan.md`。
- 单条媒体和连续消息复用 `timeline.render_line`，批次携带可信消息编号并在消费时核对会话、端点、发送者；当前批次不再重复出现在历史尾巴里。
- 前台 `view_image` 替换 `read_image_text`，默认依次查当前图、同一 actor 最近十条消息、本会话最近百条消息；指定错误/跨会话句柄不回退，找不到时明确提示 `message_handle`。视觉模型返回画面描述和文字，继续使用原受保护下载、模型预算与 `official_ocr` 计量通道。
- 新查找测试在旧实现上失败；修复后 91 项 PostgreSQL/FakePlatform 针对性回归通过，含媒体纯消息合并、隔离、配额与原模型网关；Ruff 和两项导入检查通过；该小步全套 1,417 项通过、跳过 10 项。
- 尚未接入前台原图、后台说明或视频直传；未使用真实模型/QQ，不声称已通过方案中的真实对话验收，未开 PR、未合并。

## 开发记录（2026-11-04）· 数据库租约自动恢复（未部署）

- 官方运行时遇到租约失效或驱动确认的暂时性数据库故障，关闭旧会话并排空任务后，以新租约指数退避重试（默认 2 秒，最多 60 秒）；继续使用独占锁与递增 epoch，不能绕过人工暂停、禁用、认证终态或另一实例。
- 健康检查将历史消息缺口与当前收发可用性分开；已有缺口但租约、网关和发送器正常时为 `ready_with_message_gap`，保留公开布尔警告及独立指标，租约更替不会清掉进程内警告。不声称停机期间消息已补齐。
- 新回归在旧代码上复现终止租约后永久等待及历史缺口误报。46 项针对性测试通过；全量运行 1,413 项、跳过 10 项、零失败，Ruff 与两项导入检查通过。
- 验收使用真实本地 PostgreSQL 和仅 loopback 的 FakePlatform：三次终止租约（其中一次失效 QQ 恢复点）均无需人工恢复，四轮入站与出站回执完成且不重复。它不代替腾讯实际会话和真实用户验收，也未证明跨网线路已修好。

## 开发记录（2026-11-03）· 后台任务追问与插话（未部署）

- 普通前台回合的易变上下文新增当前会话、当前用户的任务快照：活动任务优先，最多八项，含任务内容、状态、耗时、最近工具与结果摘要；数据按不可信资料引用，不进入稳定 system/history 前缀。
- 进度追问先 `agent_status`；补充、修改或回答后台问题先核对目标再 `agent_steer`；多任务指代不明时澄清。已结束任务说明结果或失败，不因催促自动重跑，也不把后台完成当作文件已送达。
- 已有活动任务时，`agent` 默认拒绝再次新建并给出原任务编号；只有明确的独立新工作使用 `independent=true`。检查与全局三任务上限共用事务锁，幂等重放仍返回原任务，不阻塞其他用户或会话。已授权的定时例行任务保持独立触发。
- 本地独立 PostgreSQL 测试覆盖前台上下文、进度查询、追加要求、重复创建防护、并发、权限隔离、已结束任务和定时任务；81 项相关回归通过；全量运行 1,404 项、跳过 10 项、零失败，Ruff 与两项导入检查通过。另以主线旧实现对比：同用户重复提交会创建两份任务，新实现只保留一份并明确拒绝第二次。模型使用测试替身，不代表真实模型已经通过对话验收。
- 未连接生产服务器、未修改线上任务、未部署。合并上线前仍须在授权测试群依次验收“制作网页 → 做咋样了 → 再加按钮 → 另外做一份图表”，核对前三步任务数不增加、补充进入原任务、最后一步独立新建；另测多任务歧义和已结束任务追问。

## 当前执行记录（2026-10-31）

- W1–W7、W9–W10 已合并并统一部署到 tank / h610，Bot 已恢复 ready；后台 PDF、最终文字及带 @ 的定时提醒均取得官方 QQ 成功回执。
- W8 GitHub 机器人功能按 owner 要求暂缓，不等待新账号/token。
- PR #31–#60 覆盖窗口和记忆、管理台清理、后台任务、定时任务、只读运维、VM 浏览器/PDF/视频、语音、消息合并、主动接话和旧代码清理，以及实测暴露的 VM 环境和发送问题。
- 当前运行 Bot `787946d`，Nix 配置 `929c6e6c`，数据库 `0077_retired_features`。完整备份在 tank 的 `/data/services/gaoji/backups/rework-20261031-final/`，PostgreSQL 17 归档已验证可读；回滚不能只切旧代码。
- 已合并原官方群会话的 85 条消息，旧群合并后共 10,285 条；窗口起点为 30,641，迁入目标群记忆 15 条。聊天、绑定、文件、原工作区和源 state_blobs 保留，仍使用的 Fleet 表保留。
- 原群 VM 和新模板已完成浏览器、PDF、视频转写实测，新模板克隆启动也通过。模板来自干净基础镜像，不含群文件或宿主凭据。
- 最终验收后台任务正常完成：在原群工作区生成一页中英文 PDF，pdftotext 可读取，文件大小 2,482 字节；PDF、结果文字和提醒共三条发送均为 committed。语音消息也已取得平台回执；61 秒 SILK 仅验证上传接受，不代表平台硬性时长上限，未要求用户确认客户端播放。
- 本次会话已授权自行审阅、使用 merge commit 合并及统一部署，不等待 CI。tank 仍为主库；因主备链路丢包，owner 明确授权将 h610 暂时改为异步备库，网络稳定后再恢复同步。不是主备角色切换。
- 全套测试曾运行 1,382 项，发现的退役测试和集成问题已修正，并通过受影响模块复测；前端构建、静态导入检查、Ruff 和两个 Nix 系统构建通过。最后两处 VM 复用和提醒发送修复通过 27 项针对性测试，没有重复扩充全量测试。
- 2026-11-01 实测发现群 VM 能连宿主回环（10.0.2.2 的 sshd、PostgreSQL）和 tailnet 服务，此前"VM 网络本来就隔离"的说法不成立。修复见 `docs/sandbox-security.md` 的 KVM networking：libvirt session daemon 移入 `gaoji-vm.service`，nftables 按该 cgroup 只放行公网、DNS 和后台中继端口 18640/18641。部署前 VM 仍不隔离。
- 当前运行说明以 README、qq-official-host-config 和 deploy-rework 为准。下面是最初的任务交接快照，旧实现位置和未完成标记不代表最新状态。

## 执行记录（2026-11-01）

- 已部署：Bot `6836fae`，Nix 配置 `dfcff7c9`。数据库没有迁移，仍是 `0077`。部署前的完整备份在 tank 的 `/data/services/gaoji/backups/codex-20261101-*/`，同目录有改动前的 `runtime.env`。
- **后台改用 Codex**（#62，修复 #66、#67）：
  - 后台任务在群 VM 里运行固定版本 0.153.4 的 `codex app-server`，第一次用时按 sha256 校验后自动安装。
  - 宿主的工具（history、web_search、web_fetch、ask_main，owner 私聊发起的任务还有 ops_read）以 dynamic tools 的形式提供给 Codex。
  - 模型请求走宿主 127.0.0.1:18640 的中转站：每个任务一个令牌，宿主换上自己的模型、推理强度和 cliproxy key，key 不进 VM。app-server 的通道在 18641。
  - 后台模型和推理强度在控制台「任务与投递」页修改，默认 `gpt-6.1-sol` / `high`。
  - 实测：原群的内存价格任务完整跑通，图、CSV 和资料包都发到了群里，数据点都附了来源。
- **VM 网络隔离**（#63）：`gaoji-vm.service` 管 VM 的守护进程，nftables 按它的 cgroup 只放行公网、DNS 和 18640/18641。部署后在群 VM 里实测：tank 本机的 SSH、PostgreSQL、bot 端口，tailnet 和局域网都拦住了；中转站和公网是通的。
- **表情包**（#64）：不指定内容也能发；GIF 能发，库里 136 张都通过了新的格式检查，发出去会动；只在能发的格式里挑，坏文件自动换下一张。
- **人设和技能**（#65）：人设和真实聊天样例放在 tank 的 `/home/kenneth/services/gaoji/persona.md` 和 `style-examples.md`，由 `AI_SYSTEM_PROMPT_FILE` 和 `AI_STYLE_EXAMPLES_FILE` 指定，不进公开仓库。reminders 和 browser 两个技能文件改成了现在的工具。
- **还没做完**：
  - 新绑定的第二个群还没开主动消息。超过 5 分钟的后台结果发不进去（`这个群没开主动发言`），要群主在 QQ 里打开。
  - h610 仍是异步备库，网络稳定后要恢复同步。
  - W8 GitHub 仍然暂缓。


> 2026-10-31 编写。接手的人（或 AI）先读完本文，再读 `docs/gaoji-rework-plan.md`（总体方案）和 `docs/deploy-rework.md`（上线步骤）。
> 原则一句话：**先管模型看到什么，删代码优先，能交给后台 agent 的就交给后台 agent，maxx 做得好的地方照着做。**

---

## 0. 开工前 owner 要给接手人的东西

接手人只需要能开发和测试，不需要生产权限：

| 要给的 | 说明 |
|---|---|
| GitHub 仓库的写权限 | 在自己的分支开发，开 PR，由 owner 合并 |
| 一个**测试用**的官方 QQ 机器人和测试群 | 在 QQ 开放平台用沙箱环境，或单独建一个测试机器人。**不要给生产机器人的 AppSecret** |
| 一个测试用的模型 API key | 本地跑真实对话时用，设置每日额度上限 |
| maxx 仓库 | `HCHogan/max` 的 `maxx` 分支。私有仓库的话要给读权限 |
| 起点 | PR #31 合并后从 main 开分支；没合并就从 `rework/p0` 开分支 |

**不给**：tank 的 ssh、生产库、生产环境的 env 文件和密钥。部署、生产库操作和停服务都由 owner 来做（§8）。接手的如果是 AI，仓库根目录的 `AGENTS.md` 会自动加载这些规则。

---

## 1. 现在在哪

| 项目 | 位置 |
|---|---|
| 仓库 | `zty20040403/gaojibot`（公开仓库） |
| 改造分支 | `rework/p0`，PR #31（未合并、未部署） |
| 改造前的快照 | tag `pre-rework-20261031`：删掉的功能都能从这里找回来参考 |
| 搜索修复 | PR #30 已合并进 main，**未部署** |
| 本地工作目录 | 原作者用的是 worktree `bot-qq-official`；主检出目录 `bot` 有别的会话在用，不要在那里切分支 |
| 参考实现 | maxx：`HCHogan/max` 的 `maxx` 分支（Rust）。设计文档 `docs/rust-rewrite.md` 写得很细，遇到设计问题先查它 |

### 1.1 已完成（PR #31）

- **只跑官方 QQ**：删除了 OneBot/NapCat 的入口、消息处理器、命令处理器和执行器。启动入口 `platform_entrypoint.py` 直接启动 `application_runtime.create_application`。
- **删除了零使用的功能**：项目工作区、例行任务、提醒、群百科、固定消息、MCP、运维/集群聊天工具、告警、旧浏览器、OneBot 的视频和语音、iMessage/Matrix 桥接、桌面 App 和打包、136 个没用的配置项。`src` 从约 14.8 万行降到约 8.1 万行。
- **新提示词** `src/plugins/ai_chat/prompt.py`：人设（生产环境的 `AI_SYSTEM_PROMPT` 优先）加群聊规则、`[silence]`、红线、说话风格、工具用法和记忆规则。
- **新上下文** `conversation_window.py` 和 `application/turn_context.py`：
  - system 固定 → `[聊天记录]`（按时间排，起点只在超出上限或 `/clear` 时移动，便于缓存）→ 一条易变消息（`[现在]`、`[记忆]`、`[引用的消息]`、`[新消息]`）。
  - 别的 bot 的工具日志和报错 JSON 会被过滤掉（`timeline.is_noise`）。
- **`history` 工具** `tools/history.py`：按内容搜索、按人和时间读原文、`around` 读前后文、翻页。
- **记忆**：每个会话一份文本（表 `conversation_memory`），工具是 `memory_append` 和 `memory_write`，每轮都带上，每天 5 点整理一次（`application_maintenance.py`）。
- **命令**：`/clear` 只移动窗口起点，不删任何数据；`/memory` 查看当前记忆。
- **`[silence]`**：模型回复这个就什么都不发（`platform_conversation.py` 的 `is_silence_reply`）。
- **表情包**：`send_sticker` 改成一步，传一句意思，从带识图标签的表情库里挑一张当图片发出；删除了 `send_qq_face`。
- **请求日志**：每轮最后一次发给模型的完整请求存在 `turn_requests` 表，保留 14 天。
- **测试**：1,678 个测试里失败 16 个，全部是 main 上本来就失败的（见 §7.3）。

### 1.2 生产环境已做的数据操作

- 已删除机器人不在的 6 个旧群的数据。删除前做了完整备份，放在 tank 的 `/data/services/gaoji/purge-backups/`，恢复要用 PostgreSQL 17 的 `pg_restore`。
- **还没做**：`docs/deploy-rework.md` 第 3 节的数据调整（合并会话、设置窗口起点、迁移旧记忆）。

---

## 2. 一轮对话现在怎么走

```
腾讯网关 → adapters/qq_official/（入站、去重、绑定、权限）
  → application/chat_orchestrator.py（记账、选模型、建回合）
  → application/platform_chat.py  PlatformChatService.ask
      ├ turn_context.turn_window → 聊天记录尾巴 + 记忆 + 引用消息
      ├ prompt.system_prompt      → system
      ├ tools/__init__.py 的 _base_registry + adapters/qq_official/media_tools.py → 工具
      └ agent_loop.ask_deepseek_with_tools(system_prompt=...) → 模型和工具循环
  → adapters/qq_official/（Outbox、按 ### 分节流式发送、[silence] 不发）
```

关键文件：

| 文件 | 作用 |
|---|---|
| `prompt.py` | 人设和规则，改说话方式先改这里 |
| `conversation_window.py` | 窗口起点、记忆、请求日志的存储（`ConversationStore`）以及 `build_tail` |
| `application/turn_context.py` | 拼一轮的上下文；尾巴上限是模型窗口的 1/8，在 8k 到 64k token 之间 |
| `timeline.py` | 一行一条的渲染格式、噪声过滤、`estimate_tokens` |
| `tools/history.py`、`tools/memory.py`、`tools/sources.py` | 查历史、记忆、搜索和网页 |
| `adapters/qq_official/media_tools.py` | 表情包、识图、语音转写、转发 |
| `tools/owned_sandbox.py`、`vm_sandbox.py` | 每个群的持久 VM 沙盒，文件在 `/work` |
| `subagents.py`、`agent/`、`adapters/qq_official/subagent_*.py` | 旧的 Sub-Agent，约 9,800 行，**要被替换** |
| `ledger.py` | 所有消息的账本，`history()` 是给 `history` 工具用的查询 |
| `migrations/versions/0073_*` | 新增的三张表 |

---

## 3. 原则（照这个判断取舍）

1. **先管模型看到什么**：提示词、上下文、记忆、工具说明的效果，比再加一层校验大得多。
2. **删代码优先**：没人用的就删，不写兼容层，不留"以防万一"的开关。删之前在生产库查一下使用量。
3. **能交给后台 agent 的就交给后台 agent**：需要多步操作、跑命令、跑很久的事情（查服务器、分析视频、写代码、开 PR、定时汇总），都不要做成前台的专用工具链，而是给后台 agent 合适的工具，让它在群 VM 里自己完成，最后把结果交回前台。
4. **工具少而通用**：按功能设计工具，不按动作拆。前台工具保持在约 20 个以内。
5. **密钥只放在宿主上**：群友能让 AI 在 VM 里执行任意命令，所以 VM 里的东西都可能被读走。token、账号和内网访问只放在宿主上，由宿主上的工具替 agent 调用。
6. **出错要大声报**：不要静默降级，不要把失败包装成"部分完成"。
7. **每个 PR 只做一件事**，手写改动约 400 行以内，用 merge commit 合并；部署和生产数据操作都要 owner 批准。
8. **用真实对话验收**：每项都在测试群里实际问一遍，看 bot 怎么回，不能只看单元测试。

---

## 4. maxx 值得吸收的地方

以下都已经在 maxx 里实现并验证过。做对应功能时先读 maxx 的代码和文档：

| 点 | maxx 的做法 | 我们的现状和要做的 |
|---|---|---|
| 工具集固定、少 | 模型只有 `run_code` 和 `shell` 两个工具，其余 Max 工具在 VM 里用 JS 调用，可以 `Promise.all` 并发 | 不照搬 codemode（要重写整个工具层），但学两点：工具少；**提示词里鼓励同一轮并行调用多个工具** |
| 前缀稳定 | 按变化频率排：system → 工具 → 摘要 → 原始尾巴 → 易变块；尾巴起点只在截断时移动 | ✅ 已做 |
| 记忆是一个文件 | 每轮注入，不放 system；明确写清楚什么时候记；往末尾追加；每晚由 dreamer 整理；整理前留旧版本 | ✅ 已做。可以补：`memory.prev` 已存在 `previous_content` 列，后台可以加"恢复上一版" |
| 一个 history 工具 | 搜索和读原文合并；按字面加按意思（向量）搜 | ✅ 字面搜索已做。**可以补：按意思搜**（向量），复用现有的 `semantic_recall` |
| 后台 agent | 在群 VM 里跑现成的 coding agent（pi）；同时最多 3 个、每个最长 2 小时、剩 5 分钟时提醒收尾；可以插话（steer）；卡住时用 `ask_max` 反问前台；结果作为唤醒交回前台，由前台用人设去说 | **见 §5 W3，这是最重要的一项** |
| 密钥不进 VM | 搜索 key、GitHub token 都在宿主上，由工具替 agent 调用 | 运维工具、GitHub 工具都照这个做 |
| 读网页在 VM 里 | `webread <url>` 命令：trafilatura 抽正文，pdftotext 读 PDF，`--offset` 续读；maxx 的 VM 网络进不了内网 | 我们的 `web_fetch` 在宿主上跑，已经限制只能访问公网。再加一个 VM 里的 `webread`，补上 PDF |
| 浏览器常驻在 VM | headless Chromium 常驻，CDP 只监听 127.0.0.1；SDK 里写 Playwright；`snapshot` 给出带编号的可访问性树；截图交给模型看 | 见 W6 |
| GitHub 开 PR | 专用机器人账号；`github_push` 收 bundle，宿主在自己的空仓库里推到 fork；`github` 调 REST，写操作只限自己的东西；每天最多 10 个 PR；PR 描述加脚注 | 见 W8，直接照抄设计 |
| 话没说完先等 | 只有 @、以逗号顿号结尾、以"然后""对了"结尾、5 秒内第三条时，等 10 秒再回（最多 20 秒） | 见 W9 |
| 主动接话精简 | 小模型判断加去抖，每小时最多 8 次，删掉复杂的闸门 | 见 W9 |
| 重启处理 | 正在跑的回合给一段宽限期，到期中断并回一句话，不在重启后续跑 | 现在有"重启中断的回合"恢复逻辑，可以按这个思路简化 |
| 文档驱动 | 设计文档里每个决定写日期和原因；改之前先看真实数据（比如"上线当天 90 个回合只写过 1 次记忆"） | 建议照做：本文和计划文档持续更新 |

---

## 5. 剩余工作（按顺序）

每项都写了目标、设计、涉及的文件和验收。设计细节允许执行者判断，但不要加本文没要求的约束。

### W1 合并部署 #31（需要 owner 批准）

1. owner 审核合并 PR #31，同时部署 PR #30。
2. 按 `docs/deploy-rework.md` 执行：备份、在 NixOS 配置里停掉 `gaoji-napcat.service`、部署、生产库数据调整、验收清单。
3. 建议同时开通博查搜索：设置 `AI_SEARCH_PROVIDER=bocha` 和 `AI_SEARCH_API_KEY`，充值 10 元让并发达到 5。
4. **待确认**：宿主配置文件（`AI_QQ_OFFICIAL_HOST_CONFIG_FILE`）里 `media.stickers` 是不是 true，否则发不了表情包。

### W2 管理后台前端清理（小）

- `admin-ui/src` 里这些页面对应的后端已删除：`WikiView`、`ProjectsView`、`McpView`、`OpsManagementPanel`、`GuardianControls`、`ContextDebugView`，以及可观测性/告警、平台桥接。删掉页面、路由、CSS 和 `api.ts` 里对应的调用。
- 删除 `src/plugins/ai_chat/admin_api/` 里只为这些页面服务的残留代码。
- 可以加一个"最近请求"页面：按 turn_id 读取 `turn_requests`，方便排查问题。
- 验收：`npm run build` 通过；每个页面都能打开，不报 404。

### W3 后台 agent 替换 Sub-Agent（大，最重要）

**目标**：删除 `subagents.py`、`agent/`、`adapters/qq_official/subagent_*.py`（约 9,800 行）以及多角色、验收合同、`partial` 状态，换成一个简单的后台 agent。提醒、例行任务、运维、视频分析、GitHub 都建在它上面。

**工具（前台用，参照 maxx）**
- `agent({task, title?})`：马上返回 `job#N`。同时最多 3 个，每个最长 2 小时，剩 5 分钟时插一句话提醒它收尾，到点中断并交出已做的部分。
- `agent_steer({job, message})`：给运行中的任务插一句话；任务在等前台回答问题时，这句话就是回答。
- `agent_status({job?})`：列出标题、已运行时长、最近调用的工具。
- `agent_cancel({job})`。
- `/status`、`/kill` 命令能看到和停止后台任务。

**怎么跑（二选一，执行者决定）**
- A. 照 maxx：在群 VM 里跑现成的 coding agent（pi），通过 stdin/stdout RPC 驱动，模型请求由宿主代理（key 不进 VM）。优点是 coding 能力强、不用自己写循环；缺点是要打包 pi，还要做 RPC 适配。
- B. 先用现成的 `agent_loop` 在宿主上跑，工具是群 VM 的 shell/读写文件、`web_search`、`web_fetch`、`history`、`say`，作为 durable job 执行。优点是改动小。**建议先做 B**，等以后需要更强的 coding 能力再换成 A。

**结果交回前台**
- 报告存到 VM 的 `/work/.gaoji/jobs/job-N.md`。
- 任务结束后唤醒前台：把 `[job#N 完成] 标题` 加上报告（超长只给开头 2000 字，并写明全文路径）作为一条新输入，前台用人设决定怎么说，需要发文件就用 `send_file_from_sandbox`。
- 投递复用现有的后台结果路径（`adapters/qq_official/task_result.py` 和 `background_result` 发送用途），它已经处理了官方 QQ 的主动消息限制。
- 后台 agent 卡在只有群友知道的事情上时，用 `ask_main({question})` 反问前台。10 分钟没有回答就按自己的判断继续，并在报告里写明做了哪些假设。

**权限**
- 后台 agent **不能直接在群里说话或做事**（发消息、发文件、撤回），只能交报告。
- 工具集按发起人决定：群友发起的只有通用工具；owner 私聊发起的可以多给运维只读工具（W5）。

**数据库**：旧的 `subagent_*` 表用一个迁移删掉（数据已经没用了，删前备份）。

**验收**：在群里说"帮我查一下最近显卡价格整理一下"：前台马上回一句"开始了"，几分钟后收到带来源和日期的整理结果；发起任务后说"顺便查一下内存"，后台任务能收到这句话。

### W4 提醒和例行任务

- 一张表 `schedules(id, scope_key, endpoint_key, actor_key, kind 'reminder'|'routine', text, next_at, cron, created_at, cancelled_at)`。
- 工具：`schedule_set({at | cron, text, kind})`、`schedule_list`、`schedule_cancel`。时间统一用 Asia/Shanghai。
- 每 30 秒扫一次到期的任务：
  - **提醒**：直接发主动消息 `@某人 提醒：…`（受官方 QQ 主动消息配额限制：全局每分钟 60 条，每个群每分钟 20 条、每天 1000 条）。
  - **例行任务**：启动一次后台 agent（W3），`task` 就是这条任务的文字，结果照常交回前台。
- 限制：每人最多 20 个，例行任务最短间隔 1 小时，记录用量。
- 旧实现在 tag 里（`reminders.py`、`routines.py`、`adapters/qq_official/native_schedules.py`），只作参考，不要搬回来。
- 验收："明天下午 3 点提醒我开会"到点能收到；"每天 9 点汇总昨晚群聊"能每天按时发出汇总。

### W5 运维只读工具（给后台 agent 用）

- 宿主上加一个 `ops_read({action, host?, unit?, lines?})` 工具，调用 tank 上仍在运行的 cluster-control 服务（`src/cluster_control/`，客户端代码可以参考 tag 里的 `fleet_client.py`）。只开只读操作：主机概况、服务状态、日志尾部、磁盘和资源。
- **只给 owner 私聊发起的后台 agent**；群里发起的任务一律没有这个工具。
- 写操作（重启、停服务）先不开。以后要开时走现有的私聊验证码审批（`adapters/qq_official/approval.py`），每次都要 owner 确认。
- 只能由宿主来调用，**不要把 ssh key 或 token 放进 VM**：群友能让后台 agent 在 VM 里以 root 执行任意命令。VM 的出站限制见 `docs/sandbox-security.md`（KVM networking），它只挡宿主和内网地址，不保护 VM 里的东西。
- 验收：owner 私聊说"看看 tank 为什么慢"，后台 agent 自己查状态、翻日志，最后给出结论。

### W6 VM 浏览器、看截图、webread

- 在 `nix/sandbox-image.nix` 里预装 Chromium 和 Playwright（还有 trafilatura、poppler-utils）。
- 照 maxx：Chromium 常驻，CDP 只监听 127.0.0.1，用户资料放在 `/work/.gaoji/chromium`。给 agent 一个小的 Python 辅助库：`page()` 取当前标签页，`snapshot(page)` 返回带编号的可访问性树。
- 新工具 `view_sandbox_image({path})`：把沙盒里的 PNG/JPEG 交给模型看（截图、生成的图表），不发到群里。
- VM 里加一个 `webread <url> [--offset N]` 命令：抽取网页正文转成 Markdown，PDF 用 pdftotext 读，每次输出 1.2 万字。
- 浏览器和 webread 都在 VM 里跑，主要给后台 agent 用。
- 验收：后台 agent 能打开需要执行脚本的网页、截图并说出页面上的内容；能读出一个在线 PDF。

### W7 语音回复和视频分析

- **语音回复**：TTS（edge-tts）生成音频 → 转成 silk → 用官方富媒体上传（`file_type=3`）→ 发出。复用现有的文件上传通道（`adapters/qq_official/file_upload.py`）。工具 `reply_with_voice({text})`，一次回复最多一条。**先在测试群确认官方接受的格式和时长上限**。
- **视频分析**：交给后台 agent。下载群友发的视频到 `/work/chat/`，用 ffmpeg 抽关键帧，用 `view_sandbox_image` 看画面，用 whisper 转写音轨，综合后交报告。前台只需要一个把群里的文件拷进沙盒的入口（参考 maxx 的 `max.file()`；现有的 `import_file_to_sandbox` 可以改造）。
- 验收：群友发一段视频说"这讲了啥"，几分钟内收到总结；说"用语音说"能收到语音。

### W8 GitHub 开 PR（给后台 agent 用）

照 maxx `crates/max/src/github.rs` 和 `docs/rust-rewrite.md` 的「GitHub」一节：
- owner 建一个机器人账号，用 classic token，只开 `public_repo`。token 只放在宿主上。
- `github_push({repo, branch, bundle})`：后台 agent 在 VM 里 `git bundle create … HEAD --not --remotes`。宿主在自己的临时空仓库里，按 bundle 头部记录的前置提交从上游浅取（`--depth=1`），开启 `fetch.fsckObjects` 收下 bundle，然后推到机器人账号的 fork（没有就先 fork）。同一个分支强推，对应的 PR 就会更新。**宿主绝不在 VM 写过的仓库里执行 git**（hooks、`core.fsmonitor` 都能执行命令）。
- `github({method, path, body?})`：读不限；写只限机器人自己的仓库、自己开的 PR/issue、自己的评论，以及开 PR（head 必须是机器人的分支）。路径只允许字母、数字和 `/-_.~`。
- 所有群合计每天最多开 10 个 PR，用 GitHub 搜索 API 计数，不另建表。PR 描述末尾自动加脚注，说明是 AI 应群友要求开的。
- VM 里的 git 提交作者设成机器人账号的 noreply 邮箱。
- 验收：在群里说"给 某仓库 提个 PR，README 加一句话"，能收到 PR 链接。

### W9 话没说完先等、主动接话精简

- 被 @ 但看起来没说完时（去掉 @ 后为空、以逗号顿号分号冒号结尾、以"但是/然后/对了/就是/所以/而且/还有/那个/因为"结尾、同一人 5 秒内第三条），等这个人停下 10 秒再回（最多等 20 秒），期间的后续消息一起当作这一批输入。
- 主动接话（`adapters/qq_official/proactive_ingress.py`、`application/shared_proactive.py`）照 maxx 精简：小模型判断加去抖，每会话每小时最多 8 次。删掉现在的概率闸门和兴趣分阈值逻辑，以及 `proactive_learning`（如果确认没用）。
- 验收：分三条发"那个""明天的会""改到几点了"，bot 只回一次，而且是回答完整的问题。

### W10 清理

- **VM 沙盒里的项目分支**：`vm_sandbox.py` 里 `project_disk`、`read_project_notes`、`_io_scope` 等分支已经不会执行；删掉这些分支后，`src/sandbox_runtime/project_*.py` 中除 `project_vm_io.host_lock` 以外的模块都可以删。注意 `vm_sandbox.py` 会按文件路径读取 `sandbox_pdf.py` 注入 VM，静态导入分析看不出来有人用它，别误删。
- **剩余的 OneBot 模块**：`onebot_codec.py`、`onebot_availability.py`、`onebot_model_output.py`、`adapters/onebot_route_context.py`、`stickers.py`（OneBot 版）、`ocr.py` 里的 OneBot 部分，以及 `pyproject.toml` 里的 `nonebot2` 和 `nonebot-adapter-onebot` 依赖。先查清引用，再逐个删。
- **工具策略**：`tool_policy.py` 里 `approval="task"`（Fleet 任务审批）的残留；`agent/registry.py` 里的 operator 角色（W3 完成后整个删除）。
- **配置和文档**：`nix/module.nix`、`.env.example`、README 里已删除功能的配置项和说明。
- **数据库**：写一个迁移，删掉已删除功能留下的表（projects、routines、reminders、wiki、mcp、fleet、migration_* 等）。执行前备份，并经 owner 同意。
- 每项清理完都要跑全套测试，并检查有没有通过文件路径引用的文件。

---

## 6. 不要做的事

- 不要恢复项目工作区、群百科、固定消息、MCP、iMessage/Matrix 桥接、桌面 App：每个群的持久 VM 加 git 已经覆盖了项目的需求，记忆加 `history` 覆盖了群百科和固定消息。
- 不要发小黄脸：官方 QQ 发不出去。
- 不要把任何密钥、token、ssh key 放进 VM。
- 不要在公开仓库里提交真实的 QQ 号、群号、openid、AppID 或内网地址；测试一律用 `TEST_…` 这样的假值。
- 未经 owner 批准，不部署、不改生产库。

---

## 7. 开发与测试

### 7.1 本地测试环境

- 需要一个带 pgvector 的 PostgreSQL 17 容器。测试库名必须以 `_test` 或 `_acceptance` 结尾，地址必须是 127.0.0.1，而且 **`AI_POSTGRES_DSN` 必须和 `TEST_POSTGRES_DSN` 不是同一个库**（否则所有存储测试会直接报错）。
- 环境变量用 `source tools/dev/testenv.sh <库名_test>` 设置，文件开头写了怎么起容器、怎么建库。
- 新库先迁移：`AI_POSTGRES_DSN=$TEST_POSTGRES_DSN python -m src.bot_storage.cli upgrade`。
- 跑全套：`python -m unittest discover -s tests -p 'test_*.py'`，本地大约 10 分钟。全套测试运行期间不要新增迁移文件，部分测试会在运行中迁移到最新版本。
- 代码检查：`ruff check bot.py src tests tools migrations nix`。

### 7.2 删代码时的检查方法

- 删除模块后，用 `tools/dev/` 里的脚本检查（都在仓库根目录运行）：
  - `check_unresolved_imports.py`：导入了已不存在的模块，包括函数内部的延迟导入。
  - `check_imported_names.py`：从模块里导入了已不存在的名字。
  - `dead_modules.py <入口模块…>`：从入口出发走不到的模块。入口不止 `platform_entrypoint`，还有 `bot_storage.cli`（`gaoji-db`）、`cluster_control`、集群 worker，都要作为入口传进去。
- 现在 `check_unresolved_imports.py src` 会报 4 个文件，都是已经不会执行的分支里的延迟导入：`subagents.py`、`admin_api/subagents.py`、`subagent_composition.py`（W3 删除）和 `vm_sandbox.py` 的项目浏览器分支（W10 删除）。除了这 4 个，不应该再出现新的。
- **还要检查按文件路径读取的文件**：用 grep 找代码里出现的 `.py"` 字符串，例如 `sandbox_pdf.py` 是按路径读的，不是 import 的。
- 用 tag `pre-rework-20261031` 的 worktree 跑同一批测试做对照，分清哪些失败是本来就有的。

### 7.3 main 上本来就失败的测试（不是这次改出来的）

- `test_coordinator_artifacts`：10 个，Sub-Agent 产物验收，W3 完成后随 Sub-Agent 一起删除。
- `test_platform_identity_migration`（3 个）和 `test_qq_official_migration_constraints`（2 个）：迁移回退时报 `direct_turn_history_requires_export_before_downgrade`。
- `test_qq_official_gateway` 的 `test_lifecycle_proactive_body_error_and_accepted_response_loss`：1 个。

---

## 8. 生产环境要点

- 服务跑在 tank 上：`gaoji.service`（bot）、`cliproxy.service`（模型代理）、`gaoji-cluster-worker.service`、`gaoji-napcat.service`（W1 时停掉）。
- 部署用 Nix；环境变量分布在 `/home/kenneth/services/gaoji/*.env`，加上 sops 渲染出的密钥文件，还有一个普通用户读不到的 owner env。**以运行进程的环境（`/proc/<pid>/environ`）为准**，原作者就因为只看了部分文件，误以为没有配置人设。
- 数据库是主备 PostgreSQL；`pg_dump` 必须用 17 版，tank 默认的是 16 版。
- 官方 QQ 的限制：
  - 被动回复窗口：群 5 分钟内最多 5 条，私聊 60 分钟内最多 4 条。
  - 主动消息：全局每分钟 60 条；每个群每分钟 20 条、每天 1000 条。
  - 富媒体上传：`file_type` 1 是图片，2 是视频，3 是语音（silk），4 是文件；base64 上传约 6 MiB 以内，更大的文件要分片上传。
- 排查问题：先在 `turn_requests` 按 turn_id 看模型实际收到了什么，再看 `turn_journal_events` 里的工具调用。
