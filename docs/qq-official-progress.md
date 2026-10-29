# QQ 官方接入实施进度

目标与验收要求以 [qq-official-plan.md](qq-official-plan.md) 为准。离线测试、目标账号实测、生产启用分别记录。

## 2026-10-28：实施准备

- 已 fetch 并核对基线：`af0fba687adf782286d52725f364b942b1efc952`。
- 分支：`feat/qq-official`；独立 worktree：`/Users/kenneth/Desktop/comp390/bot-qq-official`。Codex 的 worktree 工具因聊天根目录不是 Git 仓库返回错误，随后按方案使用 `git worktree add` 建立。
- `pre-qq-official` 已建立并推送，指向上述改造前基线；未覆盖已有标签。
- 通过本机现有 GitHub 认证核对登录名与用户 ID，匹配提交使用的 GitHub noreply 邮箱。没有修改全局 Git 配置。连接器登录账号不同，后续 PR 使用已经核对的本机账号。
- 首个提交 `8832bef` 保存完整方案。主目录中两份未跟踪计划保持原状。
- 独立 Python 3.12 环境使用 `uv sync --locked`；没有增加依赖。新建本机 loopback PostgreSQL/pgvector 测试容器，与既有测试容器分离；应用测试库和验收库互不相同。
- 本轮测试禁用 dotenv，使用隔离状态目录；完整回归运行时额外使用 Python socket 审计钩子，阻止非 loopback TCP 与外部 DNS。
- 未连接生产服务器、使用真实机器人凭证或执行真实 QQ 请求。

### 基线检查与最小修复

重构业务入口之前运行完整旧测试：3,231 项，978.456 秒，3 个失败、1 个 setup 错误、17 个跳过。逐项核对后：

- 两项文件崩溃恢复测试要求数据库名字为 `gaoji_acceptance`（另有严格的后缀规则），初始测试库命名不匹配。已在本任务独立容器内改名，并重新验证；没有放宽测试的生产库保护。
- 桌面 controller 的 Dockerfile 没有复制新加入的传递依赖 `src/browser_runtime.py`。现有隔离导入测试稳定失败；补齐这一项 COPY。没有构建/发布镜像。
- `migrations/env.py` 的 `fileConfig` 默认禁用了进程里已有的 worker 日志，导致后续异常巡检测试无法观察日志。改为 `disable_existing_loggers=False`，保留运行进程原有日志。

上述两个代码改动是使方案要求的完整回归可执行的最小前置修复，单独提交并说明偏离；不扩展项目空间功能。相关 39 项测试已分批验证通过，含真正进程中断但平台 I/O 为本机模拟的文件验收。完整里程碑回归另行记录。

## M0：协议、假平台与探测工具（离线完成，已合并）

实现范围为 `src/qq_official/`，没有接入应用启动或改变 OneBot 路径。协议包不依赖应用存储、NoneBot 或 OneBot。

采用已有 `httpx` 负责 REST，已有 `aiohttp` 负责 WebSocket 和假平台。选择 aiohttp 是为了复用同一套可注入网关传输与本机服务器，未增加依赖。时间、退避和随机抖动可注入；业务去重与持久 inbox 留在 M2 的适配器。

参考腾讯 SDK 固定 commit：[`429a0f1b798f3b0fcef3679e207dbc0469a1df2d`](https://github.com/tencent-connect/qqbot-nodejs/tree/429a0f1b798f3b0fcef3679e207dbc0469a1df2d)。本次成功读取 SDK；腾讯 WebSocket 文档页面重新抓取失败，协议差异依据方案中已有记录保守处理。沙箱地址的当前账号可用性仍待实测。

重点验证：

- token single-flight、旧 token 不延长到期、鉴权失效、重试退避；
- HTTP 200 内错误码、未知错误的接受状态、响应丢失时不自动重发；
- 网关预算、ACK、Resume、关闭码、持久回调失败时不推进恢复点；
- 事件 ID、消息 ID、引用索引分开，结构化 @ 与正文标签分开；
- 凭证文件权限、可信目标域、假凭证限定的 loopback 模式；
- 探测默认只监听，发送批次显式确认，报告脱敏、原始身份映射只写受保护本地文件。

交叉审查后已修复：本地序号缺口保留恢复点先 Resume；持久入队回调不阻塞 ACK；有界队列溢出停止并保留恢复点；并发错误优先暴露入库失败；旧连接鉴权失败只失效它使用的 token；服务端请求心跳后按真实发送时间计算 ACK 期限；重复事件不重置窗口；探测每次请求前复查窗口，结果不明后停止该端点后续探测；报告保存停止前诊断。

安全检查还覆盖真实 macOS 扩展/继承 ACL、JSON 整数与转义体积、未信任字段的 repr、冲突引用，以及 token `Retry-After` 冷却。暂不支持的 Windows ACL 与 Unix 文件系统均明确拒绝。

Linux 原生权限补充验证使用已有官方 `python:3.12-slim` 镜像，digest `sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf`，Python 3.12.13 / arm64。容器无网络、源码仅只读挂载、无宿主凭证，退出后已移除。overlayfs 上 10 项检查通过：0600、0644、真实 named-user ACL、非 POSIX ACL、移除 ACL、写入前检查、降权服务用户与专用组、无关组成员拒绝、未知文件系统拒绝。Docker 提供的独立 tmpfs 不支持 ACL xattr 查询，当前实现会拒绝；不能将其计作凭证读取成功。

TODO(qq-official)：M8 核实目标 Nix 的 `/run` 凭证文件系统，补齐可验证的无 ACL 文件系统处理及 Windows 原生 ACL。当前的拒绝行为是明确限制，不能靠关闭检查上线。

最终验证：

- 本地全量回归运行 3,332 项，1,152.748 秒，成功，19 项跳过。测试发现时包括 92 项协议测试和 9 项已准备的 M1 OneBot 快照；快照尚未改动业务入口，也不纳入 M0 PR。
- 全量回归运行期间补齐鉴权竞态等 4 项协议回归；最终源码单独运行全部 96 项协议测试，成功，2 项 Linux 专用测试在 macOS 跳过。Linux 原生权限另按上文独立验证。
- 完整 ruff、compileall、git diff 空白检查通过；两个隔离库的 upgrade/check 通过，单一 Alembic head 为 `0060_project_quota_jobs`，本阶段没有新增迁移。
- 协议 import 边界、假平台网关/REST、探测 CLI 端到端检查均通过。真实 QQ 能力仍未验收。

M0 代码已达到本阶段离线完成标准。整体方案、M1–M9 和生产启用均未完成。

M0 已推送并建立 [PR #1](https://github.com/zty20040403/gaojibot/pull/1)。首次 GitHub Actions 在解析既有工作流时失败，尚未启动任何 job：job 级 `env` 使用了不允许的 `runner.temp` 上下文。最小修复是在 step 中通过 `$RUNNER_TEMP` / `$GITHUB_ENV` 设置同一隔离状态路径；没有放宽测试或切换数据库。修复后继续检查 CI。

修复提交 `23056c4` 的 Python 3.12、Nix package、Secret scan 全部通过。HTTPS OAuth 凭证缺少修改 workflow 的 scope，推送被 GitHub 拒绝；随后核对已有 SSH 认证同属当前用户，使用已有 GitHub SSH 凭证推送，没有修改凭证权限或远端配置。用户明确回复“我已审阅，授权你用 merge commit 合并”后，核对 PR head 与三项检查并完成合并：`7ea5ac09ec5a8102c0ff899b92e13700ca5133e6`。独立开发分支已 fetch 并同步该 main，没有推送 M1 未完成改动。

## M1：共享回合与身份隔离（离线完成，已合并）

- 在改业务前新增 9 项真实 OneBot 出站快照并纳入上述完整基线；固定平台 I/O、时钟和测试身份。
- 引入结构化账号、端点、操作者、入站、能力与发送契约；旧 OneBot scope 和设置 key 保持原样。公共类型可在阻断 NoneBot 导入的真实子进程中加载。
- 原插件初始化移到显式 `onebot_plugin`，Bot、App 和 NoneBot 配置统一加载；公共包先导入后仍能正常注册原 matcher 与生命周期。
- 公共 orchestrator 接收 InboundMessage/TurnPlatform；OneBot 包装真实事件，任务注册保留显式数字兼容元数据，官方任务不进入旧数字查询/取消路径。
- 两种平台共用现有上下文、Historian、回放、工具循环和记忆写回；独立官方 composition 在禁止 NoneBot 导入的进程中，使用真实测试数据库与模拟模型完成工具、记忆、历史和 journal。未迁移工具默认隐藏且执行端拒绝。
- 新增 `0061_platform_actor_identity`，保留旧 principal 与记录，扩展账号/身份命名空间；消息保留 actor/source endpoint，绑定历史后的同字面消息 ID 仍可区分。官方个人记忆按完整 actor 隔离。
- 群级设置与后台读视图增加规范 scope key，旧 OneBot 设置仍可通过新入口读写。历史键解码集中处理，不再把官方 key 当数字群号解析。
- 绑定、帮助、模型、effort、用量、任务状态和取消已抽出公共命令分发；配置策略默认拒绝，任务仅作用于当前 endpoint/actor。OneBot matcher 继续调用原处理器，保留原始参数及 finish 行为。
- 交叉审查修复了 C2C bot 作者隔离、私聊显式引用遗漏、实际群模型默认与个人 effort 接线、后台多历史覆盖合并，以及新 OneBot 端口任意 ID 引用/撤回的边界。官方未知执行异常转换为不含原始错误信息的安全失败。

最终离线验证：

- 全量运行 3,479 项测试，1,401.134 秒，成功，19 项跳过。全部 9 项 OneBot 出站快照保留重构前的期待值，通过；未用更新快照掩盖变化。
- 测试开始后冻结代码和测试文件，以哈希复核无变化。M2 准备仅在仓库外进行，没有进入此次回归或提交。
- 完整 ruff、compileall、git diff 空白检查通过；两个隔离数据库 upgrade/check 通过，单一 Alembic head 为 `0061_platform_actor_identity`。
- 干净子进程的真实 composition 验证了实际群模型默认、个人 effort、工具、记忆、历史、journal 与任务清理，全程禁止 NoneBot 导入。真实存储续接测试核对 actor principal、实际引用端点和刷新不重复建边。
- 新传输端口的引用/撤回、并发消费和未知结果不重放测试通过。旧 OneBot 出站仍由原路径负责。

本阶段达到离线完成标准。实现提交 `a3bc6a6` 的 [PR #2](https://github.com/zty20040403/gaojibot/pull/2) 已通过 Python 3.12、Nix package、Secret scan 三项 CI；用户完成审阅后，以 merge commit `e5425370161ad665d02bf0f98ea9047149f3f502` 合并。`Delivery.target_scope` 仍是旧三字段表示，官方持久投递在 M3 补齐前不得接通；项目/宿主授权、媒体和剩余业务仍按 M4/M5/M6/M7 分步实现。M1 不构成上线或真实 QQ 能力已验证的声明。

## M2：持久入站与首次绑定（离线完成，已合并）

- 已实现事件 codec、账号独占租约、双向新群绑定、持久 inbox、引用索引和回复窗口观察，使用扩展迁移 `0062_qq_official_inbound`。账号、环境、实际端点和群/C2C 身份分别校验；不迁移旧历史。
- 接收和业务入账分为两个短事务。未绑定普通群消息不保存正文；已准入消息按端点顺序入账，并保存唯一回合意图。普通群事件后到达的同消息 @ 事件只升级触发证据，不替换正文、不刷新窗口。
- 首次 owner 仅由受保护配置或可信控制台建立。群内提交绑定码只产生候选，必须由原 owner 在另一端确认；普通绑定不自动授予群内发令者 owner。群 owner/admin 角色、昵称和首次私聊都不是本地管理授权。
- 独立 runtime 类不需要 Bot 实例；账号租约、失锁停止和 durable worker 使用本机假平台与隔离 PostgreSQL 验证。当前只保存回合意图，M3 平台发送和 M4 共享模型回合消费尚未接通，不能将唯一 job 声称为模型/工具的恰好执行一次。
- `AppContext.start_official_ingress(env)` 已接入应用启动与 shutdown 收尾；OneBotRuntime 在应用生命周期中调用，官方启动不等待 Bot connect。开关默认关闭，不读取官方凭证、不连接官方网关。禁止 NoneBot 的独立部署入口及健康状态暴露仍属于 M8。
- `.env.example` 和 `services.gaoji.qqOfficial` 按当前 `adapters/qq_official/config.py` 补齐配置。启用时必须提供 AppID、AppSecret 和独立 binding pepper 三个受保护文件；Nix 仅启用时加入官方环境变量和 `LoadCredential`，拒绝非绝对路径、Nix store 路径及带 Nix context 的文件值。现有 OneBot 的环境配置在未开启该选项时保持不变。
- private policy 默认 `owner_only`，可显式选择 `allowlist` 或 `public`；这些准入不授予管理权限。群仅允许 `manual_bind`。原始 inbox payload TTL 默认 24 小时，范围 1–168 小时；清理不删除仍影响重放的去重墓碑、消息索引和派发唯一性。
- durable job kind 按完整账号 key 的 SHA256 分开，防止一个账号的 worker 领取另一账号的消息。消息入账异常会保留可恢复任务并暂停该账号；启动和巡检发现待处理 inbox 对应已取消、失败或耗尽重试的 job 时也暂停，避免后续消息永久排队却显示健康。
- 失去 Resume 连续性时，将成员状态标为未知并废止旧窗口、敏感群授权和绑定候选；保留普通手动绑定意图。只有新的真实人类消息能重新证明接收可达；旧消息重放不恢复在群状态，明确退群撤销的绑定也不会自动恢复。
- `DatabaseCalls` 排空正在运行的数据库线程后再释放专用账号锁；DurableJobWorker 新增可选调用边界，未传入时仍使用原来的调度方式。初始化取消会等待资源构造结束再关闭，网关关闭异常也不会跳过 worker 排空和其余连接清理；暂停恢复先取得实际账号 advisory lock，不能仅凭残留 holder 字段判断占有状态。
- 原始 payload 终态且关联任务均已结束后，每分钟清理已到期内容；迟到 @ 不提升已过 TTL 的消息。绑定码只在内存 effect 中存在，发送失败、结果不明及重启遗留 sending 均取消挑战，普通消息表和队列不保存码或命令正文。

冻结前验证：158 项组合测试通过，覆盖新模块与 OneBot 启动/出站快照、SDK 导入边界、旧账本；另有 21 项 runtime 故障测试通过。默认 M0 协议栈连接真实本机 FakePlatform，完成 token/HTTP/WebSocket → inbox → durable worker → 账本/索引/窗口/唯一回合意图，假平台未收到业务发送请求。真实 PostgreSQL backend 终止、队列失败与线程阻塞均有回归；移除数据库排空注入的负对照会稳定复现停机过早的问题。

全仓 ruff、compileall、两库 schema upgrade/check、18 项 Nix 配置求值检查和本机 aarch64-darwin `nix flake check --no-build --print-build-logs` 通过。官方关闭时 Nix 环境和 LoadCredential 与 HEAD 基线逐项一致。第一次全量运行因静态复查发现关闭异常路径缺少 finally 而主动中止，不计为验收结果；修复并补回归后重新冻结代码及哈希，完整运行 **3,626 项测试通过**（1,488.184 秒，19 项跳过）。运行后核对 27 个实现、测试及配置文件哈希全部一致，fresh fetch 后 main 无新增改动；CI 随里程碑 PR 验证。**M2 达到离线完成标准。** 此次离线回归未使用真实 QQ 凭证；后续获得单个测试群的真实接入授权，结果在下文单独记录。生产启用尚未验收。

TODO(qq-official)：M8 在目标 NixOS 上实测 `LoadCredential` 的文件系统、服务用户读取权限与有效 ACL 验证。配置可求值不代表凭证可以被运行时接受；不放宽 ACL 检查来绕过部署限制。

M2 的 [PR #3](https://github.com/zty20040403/gaojibot/pull/3) 最终 head 为 `1b7f05df790eb958913ec0783db3366daa89254e`，Python 3.12、Nix package、Secret scan 全部通过。Secret scan 曾命中测试隐私泄漏断言中的纯合成字符串；已改成明确测试标记，仅对历史提交中的这一条指纹添加说明性排除，未放宽扫描规则。用户完成审阅并授权后，使用 merge commit `85c943e261c64bd1f3980f41b33c6a94de0a0df4` 合并；独立开发分支已同步 main。

## M3：持久发送（离线完成，已合并）

已实现冻结发送计划、原子窗口和额度预留、分片顺序、单次传输、明确拒绝后的有限重试、结果不明隔离，以及回执、账本、索引和 journal 的事务提交。复用 DeliveryStore；旧投递 worker 不能领取或修改官方记录。完整离线回归已通过，单群真机验证单独记录；其他目标能力和生产启用仍待验收。

阶段边界：M3 发送器默认只用原消息的已验证窗口；新窗口回退、主动能力探测仍关闭，不能从单条成功推断任意窗口或主动发送可用。`/bind whoami` 的受限身份答复随 M4 命令入口接线，当前不能声称已可用。M3 已实现自身撤回与群成员撤回的独立资格策略，要求实际消息端点/作者/原始时间、当前有效能力、核实过的时限及本地精确授权；未知即拒绝。按方案 3.6 节，实际 DELETE 传输和目标权限将在 M7 分别验收，当前撤回没有启用，也不会作为发送失败的自动补偿。撤回时限必须使用已验证平台原时间或自身消息的派发起点，不能用迟到回执的观察时间延长期限。IP 白名单专用错误码尚无经当前契约验证的映射，未知错误保持隔离，不能声称已实现可靠的出口 IP 诊断。

### 最终离线验证

- 全量运行 **3,952 项测试通过**（2,018.567 秒，19 项跳过）；测试期间冻结 42 个实现与测试文件，结束后全部哈希一致。M4 草稿仅在仓库外准备，没有进入本次运行或提交。
- 最终 HTTP、错误分类、发送器、迟到回执和 runtime 定向回归 129 项通过（2 项平台相关测试跳过）；TTL 和撤回资格边界另有定向验证。真实本机 FakePlatform 验证默认协议栈自动发送、回执恢复和共享账本/journal，未依赖 OneBot Bot 实例。
- 完整 ruff、compileall、git diff 空白检查通过；两个隔离数据库 upgrade/check 通过，单一 Alembic head 为 `0063_qq_official_outbox`。
- 本机 aarch64-darwin `nix flake check --no-build --print-build-logs` 通过；本机未执行 package build，Linux package 由 PR CI 验证。
- staged patch 的 Gitleaks 扫描无泄漏；受保护的真实凭证和身份未出现在变更文件中。完整测试禁用 dotenv 并阻止非 loopback 网络访问。

发送前的令牌准备与最终发送分开：令牌快照绑定当前客户端、目标与代次，单次消费；等待频控或传输槽后若已失效，只按可证明未出发处理，不在预留资格后等待刷新。高优先级回复可以替换尚未预留发送尝试的低优先级等待者，不中断已准备或正在发送的请求。已收到但未提交的回执按稳定游标周期扫描，前一页无法应用的回执不会永久挡住后页；只补记证据，不重新发送。

### 经用户授权的单群真实接入验证

用户明确允许接入其官方机器人，并指定唯一测试群；随后在该群发送唯一口令，将真实网关事件与指定目标对应。凭证、原始账号/端点/操作者身份、事件和审计 schema 名只保存在仓库外的受保护本机文件中；本记录不保存真实标识。

- 真实 token、gateway 和账户自读成功；经认证的 READY 取得真实 bot 身份。账户自读的成功响应不一定含 `bot` 字段，已按固定官方契约窄化校验：仅该认证接口允许缺省，存在时仍须严格为 true；READY 仍要求 true。
- 收到测试口令的事件类型为 `GROUP_MESSAGE_CREATE`。最初仅匹配 `GROUP_AT_MESSAGE_CREATE` 的临时接收器未命中；后续按唯一口令与实际端点确认。不能仅凭该事件类型判断用户是否 @，M4 仍须按结构化触发证据处理。
- 用当前 OfficialOutbox + OfficialSender、真实原始时间窗口及已验证 bot 身份，在独立本机数据库 schema 中发送一条纯文本回复。没有使用直接发送捷径、主动发送或新窗口回退；未创建本地 owner 授权。
- 唯一一次消息请求被平台接受；delivery 与 attempt 均为 committed，保存一条回执及各一条入站、出站账本记录。用户随后明确确认在指定群看到该回复。
- 验证完成后关闭接收连接并释放账号租约；未上传文件、重试消息、连接生产服务器或部署服务。真实测试范围仍只限用户指定群；其他能力未因此获得验收结论。

M3 的 [PR #4](https://github.com/zty20040403/gaojibot/pull/4) head 为 `8bccdf8ec969d0a1b838ef22a12e4738fc1402a1`，Python 3.12、Nix package、Secret scan 全部通过。用户随后明确授权“以后自己审阅，合适就合并”。核对实现审查、全部验证与 PR head 后，以 merge commit `516cc0b25cc437e23ca4ba4ede2700d8fd24956b` 合并；开发分支已同步 main。后续里程碑沿用此审阅和合并授权，真实测试范围及部署权限不变。

## M4：共享回合、首批命令与文字后台任务（离线完成，已合并）

- 在既有持久 job 上增加执行记录，与 journal、结果和发送计划关联；没有第二套模型循环。`prepared` 才能启动执行，持久化后的结果可恢复交接；执行中失锁或进程中断进入需核对状态，不自动重跑不确定的模型或工具操作。
- 迁移 `0064_qq_official_turn_execution` 保存规范化 `turn_text` 及版本。TxA 按真实结构化 `is_you` 证据剥离机器人前缀，TxB 与账本一同提交文本；旧 NULL 输入不从原始 payload 重建。绑定码和身份控制命令不进入普通模型上下文。
- 账号绑定的 TurnPlatform 校验原始账号、端点、actor、路由与租约；最终文本只交给 M3。默认关闭流式发送、进度、文件和主动/新窗口探测。当前连接、凭证、绑定、准入、发送机会分别判断。
- `/bind`、帮助、模型、effort、本人用量、本人任务查询与取消复用既有控制入口和共享命令分发。个人设置按实际 endpoint/actor 隔离，群默认只允许当前本地 owner 修改。其余命令明确告知暂不支持。受保护 whoami 发送/审计保留原文，普通账本采用明确脱敏占位并复用 command 排除机制，避免后续模型摘要带出身份。
- 复用现有 SubAgentCoordinator、任务/步骤/会话和检查点，官方任务使用结构化账号/端点/actor；只开放文字研究、分析和只读上下文工具。独立官方 worker 不需要 OneBot Bot 实例，结果经 M3 持久发送；取消、恢复和命令边界仍复查真实任务所有者与当前执行租约。
- 每次模型物理请求及回退前检查 job/account 租约；受保护执行禁用 SDK 隐式重试。共享接口保留未传入官方生命周期时的旧行为。

当前已经通过的定向验证包括：真实本机 FakePlatform 群 @ 和 C2C → 共享模型工具回合 → 正确 Markdown/msg_id/msg_seq → 持久 HTTP 回执；命令经真实发送器交付；超过原 5 分钟窗口的实际 Sub-Agent，在测试显式开放的主动策略下发送，拒绝后等待真实新消息窗口再发送；模型请求边界、取消、PG backend 丢失、事务回滚和冻结结果恢复。最终完整回归和真实帮助命令验证见下文；模型及后台任务的真机验证、生产部署仍未完成。

用户授权的同一测试群已完成真实 `/help` 验证：实际 `GROUP_MESSAGE_CREATE` 含 `mentions.is_you=true`，经生产 codec、TxA/TxB、共享命令路由及 M3 发送器处理，恰好一条 Markdown 帮助回复取得真实回执并提交账本，用户确认看到了该回复。测试没有模型调用、文件上传或 owner 授权，结束后关闭连接并释放租约。第一次临时测试脚本遗漏允许合法的 `message_reference` 字段，在 HTTP 发出前拦截；保留原始不确定记录，没有重放。修正脚本并通过 4 项真实 PG→M3 载荷回归后，使用用户新发的消息和独立 schema 完成上述验证。

第一次 M4 全量运行 4,139 项（1,414.058 秒，19 项跳过），仅发现一处旧 fleet 测试的兼容错误：旧任务对象没有新增的 `endpoint` 属性。修复采用兼容读取，不修改旧测试期望；相关 100 项测试通过。随后重新冻结 64 个实现和测试文件，最终全量 **4,139 项测试通过**（1,439.072 秒，19 项跳过），结束后全部哈希一致。完整 ruff、compileall、空白检查通过；两个隔离库的 schema upgrade/check、Nix flake 求值检查及 staged patch 的 Gitleaks 扫描通过。fresh fetch 的 main 与 M3 合并基线一致。M4 已达到本阶段离线完成标准。[PR #5](https://github.com/zty20040403/gaojibot/pull/5) 的 Python 3.12、Nix package、Secret scan 均通过；按用户“以后自己审阅，合适就合并”的授权审阅后，以 merge commit `606b01660b139bd182c6d1623016827fc879f01b` 合并。分支已快进同步，未把后续 M6 改动加入该 PR。

## M6：文件出口与生命周期（2026-10-29，离线完成，已合并）

实现已完成本阶段离线验证并通过独立 PR 合并。隔离数据库、内存工作区和 loopback 假平台验证与下述受控真实上传、文件消息和人工下载确认分别记录；固定小文件测试不代表其他尺寸、端点或自建下载服务已验收。

- 产物采用独立私有 CAS 存储，先固定来源、实际端点、操作者、SHA-256、大小、截止时间和草稿原因，再进入已有 durable file job。来源与模型给出的路径、摘要、URL 分离；不因父回合正常结束丢失已登记产物，撤销、换群或账号失锁仍阻止新副作用。
- 官方前台接入现有共享模型循环的七个受限工作区工具；后台使用现有 Coordinator 的捕获、独立验收和交付钩子。文件路线未知、配额不足或无安全导出能力时不开放工具。项目文件仍待 M5 授权闭环。
- 原生上传与消息发送分开。worker 先保存一次上传意图，私有保存 `file_info`，再交给既有 M3；只有真正 applied 的消息回执能推进 `message_accepted`。未知上传、发布或发送不自动换凭据/序号重发；历史回执可在撤销或凭据过期后结算事实。
- 已实现只读自建下载后端，但不自动开监听或部署。256 位随机 token 的摘要与不可变发布元数据入库，原值保存在受保护本地凭据文件；签发绝对有效期至多一小时，需合法发送条件就绪。URL/原生凭据不进入普通聊天上下文、SQL 发送正文或日志。下载完成计数不等于用户阅读确认。
- 原生普通文件的 worker 当前只启用有界小文件路径；大文件在已配置且准入的链接路线交付。分片协议与中断测试已有实现，但生产 signed PUT transport 默认关闭，未验证服务端返回地址时不猜测域名。原生草稿因 type-7 caption 可见性未验证而保持关闭，链接消息显示完整未完成原因。
- `0065_qq_official_files` 为唯一新增 head，兼容旧 OneBot/OTP 约束；已有文件证据或文件任务时拒绝直接 downgrade。新增发布意图固定原 request key/绝对期限；后台任务区分 `official-text-v1` 与受限 `official-files-v1`，旧文字任务不因新版本扩权。
- 宿主组合由显式 `FileServiceConfig`/factory 注入，默认未配置时零文件资源初始化。Nix/环境变量和生产独立服务入口仍在 M8 接线，不把测试 factory 当成已经部署。
- 增加既有管理员会话保护的控制台 GET/HEAD 取件接口。原生/链接路线失败时，可读取仍有效的持久副本；它不签公共 URL、不生成发送授权，也不把取件视为群消息接受。请求前及每块传输检查会话版本/角色、账号租约、真实来源、端点绑定、任务撤销、期限和不可变哈希；取消、过期、跨账号或损坏均拒绝。

已完成的定向批次包括：24 项真实迁移测试；15 项真实凭据/计划/晚回执测试；92 项 HTTP、原生上传和 file worker 测试；文件运行时/物理额度等六模块 93 项；前台网关→共享循环→文件 worker→M3 回执 2 项；真实 Coordinator/Sub-Agent 8 项。最后共有路径、迁移和组合复查 74 项通过；服务/只读取件 30 项、控制台与旧管理 API 27 项通过。独立内容验收与文件消息状态分开展示，新增报告回归及后台组合 11 项通过。各定向批次存在重叠，不相加冒充全量结果。

发现并修复的边界包括：任务 `cancel_requested` 在状态切换前立即禁止登记产物；SQL NULL 不绕过 protected digest 约束；不同账号/端点/操作者不能交叉关联计划；上传错误码与非空消息 ID 冲突时保留未知、禁止链接回退；真实浮点时钟可用且拒绝 NaN/bool；待验收捕获也须计入私有目录物理配额。

2026-10-29 00:53 UTC，在用户已授权且由此前真实入站证明的唯一测试群，执行独立、上传专用的受控接口探测：固定 627 字节有效 PDF 与 180 字节 ZIP，均 `file_type=4`、`srv_send_msg=false`；一次 token 请求、两次上传，均 HTTP 200 且返回有效 `file_info`/`file_uuid` 和 `ttl=86400`。受限 transport 拒绝其他目标、消息接口、不同内容、重复上传；执行前 4 项 guard 测试通过，调用前持久化一次性锁存报告。真实身份、密钥、回执句柄未写入仓库；句柄没有用于后续消息发送。此结果仅证明当时该账号/群对这两个小文件的上传能力，远端缓存 TTL 也不改变本地最长一小时交付副本政策。

首轮全量运行 4,530 项（1,623.368 秒，19 项跳过），发现一项旧测试夹具错误：M4 迁移测试直接回退 0064，跳过新增 0065，因此正确触发外键依赖保护。修复仅把该测试 helper 改为真实 Alembic 链按序退到 0063/升到 0064；没有修改已发布迁移、外键或旧测试断言。此轮不计为通过结果，修复后重新进行里程碑全量验证。

上述上传探测为零消息请求、零 WebSocket、零业务数据库写入，未将旧入站重放为新的回合或回复窗口。用户休息期间没有请求人工配合；醒来表示可以手动发消息后，另行执行下述完整小文件交付测试。

2026-10-29 11:16 UTC，使用已合并 M6 commit 的冻结代码、独立本地 schema 和受限临时连接，等待同一已授权群、同一实际操作者的新入站口令。真实 `GROUP_MESSAGE_CREATE` 的结构化 `is_you=true` 触发生产 M2 → 固定合成文件来源 → M6 文件 worker → M3 Type 7 交付；两次上传、两次消息请求均成功，两条回执均 applied，两个产物均为 `message_accepted`，零模型请求、零新增 owner grant。测试前，同一脚本的真实 PG/假平台全链与防重放/载荷约束共 3 项测试通过。上传和消息在物理请求前分别持久化一次性记录，未知结果不得重放；测试结束即关闭连接。

用户随后确认 PDF、ZIP 均能下载并打开。预期 PDF 显示英文测试文字、ZIP 内有 README.txt，但用户没有逐项复述内容，因此不另标内容核对已完成；也不伪造平台下载计数。尚未取得下载副本进行 SHA-256 比对。源 PDF 为 627 字节、SHA-256 `ddf1f4b56e0fc6adb2add765facba0f6e4e9e4ebd983ba57bd443ab1b0b7014f`；源 ZIP 为 180 字节、SHA-256 `a8fc695808e40a8e578af9d7f5214b58bad0830a7a70f00e158ed83d08265b6a`。其他端点、大文件、链接服务与真实保留期限仍待验收，未因此启用生产文件路线。

本次小文件使用两分钟固定截止。到期后，用同一 M6 版本的维护入口及真实本地账号租约执行清理：两个 CAS 副本均实际删除，剩余字节和对象数均为零，两条 applied 回执和 committed 交付记录保留；维护过程没有网络请求。此结果验证本次短截止清理，不代表自建链接的一小时可用性或腾讯远端副本删除已验收。

修复迁移测试链后，相关 47 项测试通过。重新冻结 76 个实现与测试文件，最终全量 **4,530 项测试通过**（1,638.719 秒，19 项跳过），运行后全部哈希一致。全仓 Ruff、compileall、空白检查、两个隔离数据库 schema upgrade/check、Nix flake 求值检查及 staged patch Gitleaks 扫描通过；本机未构建 Linux package，交由 PR CI 验证。M6 达到离线完成标准，默认关闭与显式宿主注入边界保持不变。

M6 的 [PR #6](https://github.com/zty20040403/gaojibot/pull/6) 最终 head 为 `1ffa4da73eeb58e64b18cc4fb66e07fc3cdf66cd`，Python 3.12、Nix package、Secret scan 全部通过。按用户既有自审合并授权审阅后，使用 merge commit `0e4a6e2ba015da72f2ba7c4bd42b5ea4e2e8c64e` 合并；开发分支快进同步，后续 M5 工作未推入该 PR。

## M5：项目授权、历史迁移与回退（已合并）

已整合项目授权、迁移日志、公开历史读取、管理员审批、旧任务来源及原生/迁移文件交付。以下是已通过的定向批次，批次有重叠，不能相加视为全量测试结果。

- 项目权限与后台执行的 170 项组合测试通过；项目文件的 8 项真实 PostgreSQL 测试包括既有 IO → M6 文件 worker → 本机假平台上传 → M3 Type 7 消息及回执。上传后撤销原 grant 时，实际消息请求为零。文件副本计入项目会话容量，重复登记保持唯一，失败时源绑定、manifest 与 job 一并回滚。
- 公共历史 64 项组合测试通过，包括真实共享模型循环的 `context_search` → `context_expand` → 最终回答，以及清除后在下一次模型 RPC 前拒绝已缓存快照。只读取原公开历史，保留原作者与来源，没有合并私人记忆或重写旧原生消息 ID。
- 管理员审批/宿主操作与旧路径的 116 项组合回归通过，另有 3 项边界回归通过。真实 TxA 分类和 Gateway receipt 参与确认，验证码不进入正文或模型；实际 HTTP 前提交持久 claim 并释放数据库锁，晚到回执只能补记事实。
- 旧 OneBot 来源及晚到回执的 27 项组合测试通过。真实 handler、任务、例行、提醒和投递保存原来源；缺来源的受管后台模型默认拒绝。已结束任务的最终文字结果可在原来源期限内登记到原队列，不授予模型/工具权限、不按重启延长期限。冻结后拒绝新投递；真实已发送且唯一匹配的群 self-echo 仍可补记旧 attempt。无可靠私聊目标或多条相同未决发送时不猜测归属。
- 单一未发布迁移 `0066_qq_official_migration` 已用于正式测试夹具；截至当前整合点，升级、降级、再次升级的表、列、约束、索引、函数和触发器目录逐项一致。降级先锁定受影响表，有任何新授权或迁移证据就拒绝丢弃。固定本机测试库只通过真实 Alembic 链刷新，没有绕过证据保护。

进一步整合结果：后台项目真实 Coordinator → 每步骤隔离 → CAS 捕获 → 独立验收 → 发布 → 物化/M3 plan 的 7 项测试通过；模拟发布提交后中断，恢复不重跑模型或重复读取捕获。项目文件新增真实 IO 回执与发送并发锁序测试，共 9 项通过；晚到 self-echo 改用真实 `mark_ambiguous` 路径验证，精确原 attempt 同步为 committed，保留历史 unknown 观察。受保护宿主配置与实际应用启动的 24 项相关测试、7 项配置/真实 PG 启动测试通过，默认不启用、不读取休眠配置，不自动迁移历史。配置见 [宿主装配说明](qq-official-host-config.md)。

控制面到领域 prepare/activate 已通过 26 项组合回归，实际提醒/周期例行/Sub-Agent 原子准备并在完成迁移后释放现有任务。OneBot 安全延期恢复的 51 项及既有 worker/SDK 延迟导入的 116 项回归通过：旧 proof 和旧 finally 不能使用新 worker 租约，已有例行正文恢复不重新获得模型执行权限。旧 Sub-Agent／项目文件的 8 项完整回归通过，独立迁移 item/import job 读取真实原 CAS，再走 M6 上传、M3 Type 7 与回执；原 hash、大小、期限和映射固定，已发/unknown 隔离。另有 38 项文件兼容、23 项预览回归通过。普通任务内已有旧产物不会自动变为新步骤输入，需作为明确 file item 单独审核。

项目迁移 grant 已接真实 cookie/CSRF 控制面、当前 owner/AdminBinding 与精确映射，预览返回固定 grant ID/revision；创建阶段只预留未来路由 epoch，迁移完成前不能执行。该部分与 console/schema 的 24 项组合回归通过。已补显式 CAS 重授、旧版本快照及 refresh 后绑定，并修复控制面与安全服务的锁顺序；46 项组合回归通过，恢复旧锁序的 mutation 测试能触发实际数据库锁超时。此重授仅用于尚未开始或 planned 的迁移，迁移完成后的通用项目授权命令仍归 M7。

单一未发布 `0066` 已纳入旧文件来源、项目待生效授权、官方项目焦点、不可修改的 IO 来源记录，以及项目任务产物的双重来源关联。最新 273 条升级／398 条降级及再次升级目录完全一致；两个固定本机测试库通过真实 Alembic 刷新。正式 schema 的来源组合、文件兼容和授权组合 48 项通过，另有 27 项授权复用/实际 grant/schema 回归通过。这里的结构准备不代表项目续接领域已装配完成。

官方前台的真实共享回合 → 项目创建 → 既有 Unix RPC 工作器 → 文件写读 → M6 → M3 Type 7 回执已通过。另一个回归在模型已读取当前项目资料后撤销原 grant，阻止后续模型调用和 IO；43 项共享回合组合及最后 21 项宿主装配/架构检查通过。这里使用本机假模型、假平台和受控工作器，不代表 Docker 内核或真实 QQ 项目交付验收。已修复旧项目别名 IO、配额作用域、焦点选择和完成回合自动保存；正式 schema 下 6 项新增完整链及 34 项官方权限回归通过，旧路径 84 项通过。UPDATE/DELETE 原 IO 授权记录均被正式触发器拒绝，快照读取到实际写入字节。

验证入口已接真实 cookie/CSRF HTTP 控制面；运行时只把已应用且 committed 的文字/文件回执登记为迁移验证，随后才允许完成迁移。11 项控制面组合及最终 4 项完整验证通过，重复请求不增加发送计划，过期审阅、伪造来源、错误 CSRF 被拒绝。任务来源路由依据实际 dispatch → migration item 类型选择原领域；20 项组合回归覆盖恢复、取消、实际 M6/M3 文件链，单条任务撤权只取消该交付。项目发布容量核算改用原资源作用域，27 项 PG/CAS 回归通过。

后台本地产物生成使用原 EXECUTE 授权，仍要求原 grant 含 DELIVER；物理上传和消息发送另查近期接收者证据。证据暂缺时保留同一 artifact/plan，在原期限内等待真实新入站；撤权、原授权到期或来源变化仍终止。91 项阶段回归及 1 项新增明确拒绝后链接回退通过，包括上传已确认后复用原凭据、链接已发布后恢复原记录、尚未发出的尝试保留历史证据，以及结果不明时禁止重发。等待不延长原窗口或截止时间，新窗口回退仍需现有显式策略开启。

旧项目任务续接已正式装配。实际 HostConfiguration 到 ProjectRuntime 的 15 项组合回归通过，包含受保护控制台 stage/plan/switch、真实 VerificationHost 请求与 loopback 文字/文件回执、迁移完成、领取原任务及 factory router/provider；撤销原 grant 后同一入口拒绝。项目续接测试保留原执行合同、内部更新 ZIP 与单独请求的 PDF；16 项续接测试已在首轮全量中通过，完整链包含真实文件 worker 等待新入站窗口、上传和 M3 Type 7 回执。真实推进时钟和原 600 秒预算保持不变，不通过延长期限或刷新来源材料消除失败。

M5 首轮全量运行 5,039 项（3,416.585 秒，19 项跳过），发现 11 个错误，不能记为通过：8 项旧配额测试把独立 QuotaAuthority 误当成聊天 ProjectAccess；2 项旧项目任务恢复把 OneBot 的结构化入站误归为官方身份；另 1 项绑定测试假定验证码只能使用创建命令的回复窗口，同秒多个有效窗口时漏认已经提交的正确回执。前两处修复保留原管理员审计及 OneBot Event 身份检查，绑定夹具改为按 challenge 关联实际已提交回执。97 项关联回归中先通过 96 项，修复最后的夹具后，绑定、发送器与任务输入的 34 项回归全部通过。

修复后重新冻结 199 个实现、测试及配置文件，最终完整回归 **5,039 项通过**（3,527.464 秒，19 项跳过），运行后全部哈希一致。既有 OneBot 出站快照、配额、任务恢复与 SDK 隔离边界通过，未更改原快照期待值；实际项目续接完整链保留原 600 秒执行预算。Ruff、compileall、Nix flake 求值及全部 202 个待提交文件的 Gitleaks 检查通过；全量结束后两个隔离数据库再次 upgrade/check 通过。暂存新文件时发现 10 个测试文件多余的结尾空行，仅规范结尾换行并逐文件比较 Python AST 不变，最终暂存差异的空白检查通过。M5 达到本地离线实现标准，PR 当前 head 的 CI 尚待验证。没有连接生产服务器、部署服务或迁移实际历史。M5 测试均使用隔离本机 PostgreSQL 与假平台；用户醒来后的独立真实文件测试使用已合并 M6 版本，结果记在上节。

M5 代码提交 `278286fdf3062c3f0a11597abe54bad4d790f759` 的 [PR #7](https://github.com/zty20040403/gaojibot/pull/7) 已通过 Python 3.12、Nix package、Secret scan。按用户“以后自己审阅，合适就合并”的授权复核后，以 merge commit `81d1813284567b8636121c836b0f606e5360df5b` 合并，开发分支已 fetch 并同步 main。合并未触发生产部署或实际历史迁移。

## M7：其余业务与可选能力（离线完成，已合并）

三个独立候选已按冻结文件哈希及共同 M5 基线整合，共 84 个实现、测试与迁移文件；公共 runtime/host/工具注册冲突逐项合并，没有用某一候选覆盖另一个候选。新增单一 `0067_qq_official_capabilities`，继续复用既有 jobs、routine runs、M3 delivery plans、文件 CAS 与群百科页。

- 命令：官方 `/例行`、`/群百科`、`/memory`、项目及显式项目 grant 命令接到真实 M4 命令生命周期与 M3 投递。原 OneBot handler 保持既有入口及返回行为。迁入项目新增不可变资源来源，新的 owner 授权逐版本关联该来源；旧迁移 grant 仍检查原映射及其版本，不能因新授权记录存在而绕过旧映射撤销。
- 后台：原生提醒/周期例行具有明确部署成本策略及绝对截止，执行与交付分别校验；重启不刷新预算。主动发言复用既有评分器，只允许当前实际来源，原入站 30 秒后丢弃，没有窗口或限流时不进入延迟插话队列。近期发言者最多查询 500 条可见消息，明确不是完整成员名册，也不授予项目权限。
- 媒体：OCR 使用既有视觉模型及真实 UsageStore；腾讯 ASR 文本或配置的本地转写保留外部数据边界。原消息引用按实际 endpoint/actor 查询，递归转发有体积与深度限制；表情文本化，媒体库 PNG/JPEG 贴纸经 M6 type 1 上传与 M3 type 7 发送。图片和普通文件分别需要对应类型的端点能力证据，旧 type 4 证明不能授权图片。
- 读取边界：HTTPS 下载固定解析目标、拒绝不安全地址与不受控跳转，并限制时间/字节；当前文档阅读支持受限文本、Markdown、JSON，不宣称 PDF/ZIP 通用内容解析已经接通。群管理、撤回、成员事件、新人简报、反馈和语音输出缺少已验证物理契约，保持明确不可用。
- 迁移：0067 新增六张来源/关联表并扩展图片 MIME 约束。降级先锁表，存在任何新来源、授权关联、后台观察或图片历史记录均拒绝，包括已取消的任务和已清理字节的图片。真实 0066→0067→0066→0067 的列、约束、索引、函数和触发器目录一致，旧 PDF、文件任务及字节记录保持原值。

整合审查发现主动发言完成独立来源校验后，继续进入 M5 proof selector 会被错误拒绝。已改为各来源分支负责完整校验；5 项实际 SourceAdmission/NativeSources/MigrationPlanGuard + M2 + M3/FakePlatform 回归通过，含 epoch 变化发送前拒绝、零 attempt。移除后台配置仍保留持久计划的拒绝校验，不让已存计划绕过原来源边界。

正式新增模块 183 项测试通过（70.051 秒）。关联旧路径 112 项批次中 111 项通过，1 项旧夹具缺少新增 MIME/类型化证据端口；补齐固定 PDF 元数据及类型化端口后，文件服务、实际宿主装配和迁移的最终 38 项通过（31.297 秒）。新增迁移 SQL 测试最初遇到复制生成列的夹具错误，改为仅复制非生成列后，验证真实图片约束与降级保护通过。已有 OneBot 快照期待值未改动。

实际无迁移宿主的命令链另发现准入 callable 的身份误判：分别读取 `store.admitted` bound method 生成不同对象，触发业务命令来源拒绝。改为同一回合只捕获一次准入 callable，再交给 lifecycle 和 execution binding，严格来源身份比较保持不变。15 项正式回归通过（23.281 秒），包括原 `/help`、模型设置、用量等命令的 M2→M4→M3 真协议链；零模型调用。首次全量尚未完成时因该独立确认的实现缺陷主动中止（exit 254），冻结文件在中止前全部保持原哈希；该轮不记通过。已清理终止 PID 所有的两个遗留测试 schema，修复后重新冻结并完整重跑。

冻结 1,118 个实现、测试及构建文件后，全量完成 5,228 项（3,383.365 秒）：5,207 项成功、19 项跳过，两项旧测试未通过；结束时全部文件哈希一致。失败分别是启动夹具缺少生产已有的 `media_factory`/`background_factory`，以及共享注册表期望值未包含新增 `recent_speakers`。已补齐工厂传递断言和工具注册期望，并验证旧 OneBot 回合仍不开放该官方工具；该次全量不记为通过。

独立审查另外发现错过执行期限的原生例行只取消 occurrence、留下 queued run，导致以后周期一直被阻塞。修复在同一事务同步结束领域 run/提醒，保留周期来源及原绝对截止；四项真实 PostgreSQL 回归覆盖下一周期可执行、到期提醒不发送、已生成结果仍按原交付期限发送、不确定执行不重跑。同时把迁移目录快照从仅比较 StoreRow 列名改为比较真实列值。上述修正正式应用后，工具注册、旧 OneBot 快照、宿主配置、真实迁移、原生调度及主动发送等九模块共 **83 项通过**（44.856 秒）。

Ruff、compileall、暂存差异空白检查及两个本机测试库的 upgrade/check 已通过。最终 89 个暂存文件的 Gitleaks 扫描为零发现。最终实现提交 `b21b9dc0fec1727ca7cb707944602c49b651596b` 的 [PR #8](https://github.com/zty20040403/gaojibot/pull/8) 已通过 Python 3.12 完整单元/集成回归、Linux Nix package 和 Secret scan。按用户的自审合并授权复核后，以 merge commit `389692aa66c2282f1a261767aa335a6ec9636ba1` 合并，独立开发分支已 fetch 并同步 main。该 CI 结果对应最终修正后的提交，不把前述有两项失败的本地全量改记为通过。以上均为本机假平台/模型与独立 PostgreSQL 证据，不代替本账号媒体、后台任务或迁移验收。

## M8：交付入口与控制台（本地完整验证及四项 CI 通过，已合并）

M8 在独立快照完成最终完整验证后，145 项变更已按冻结哈希精确采用到基于 M7 merge `389692aa66c2282f1a261767aa335a6ec9636ba1` 的正式开发分支，包含启动、凭据、App/控制台、配置、下载服务和诊断详情。本节按实际结果回填文档；PR #9 最终 head 的四项 CI 全部通过，已以 merge commit `d20fd84586efd0ef0929d34d721971bf2c1519c6` 合并，没有部署。单一 `0068_qq_official_operations` 合并本地控制台身份、官方管理员挑战与原生上传会话；降级拒绝丢弃本地管理员、任意挑战历史、未匹配挑战的身份控制消息和任何分片阶段/session 记录，尚未应用目标部署。

配置候选的 53 项回归通过（12.184 秒），23 项真实 NixOS 模块求值断言通过。渲染长度/分片、固定等待截止、认证与更严格速率配置接到实际 turn/sender；项目授权秒数由可信启动时捕获，同时限制原生命令/创建及控制台新迁移授权，原有授权和恢复任务不自动续期。默认期限保持 86,400 秒，允许配置为 60–86,400 秒。

启动候选覆盖官方单平台无 OneBot SDK 依赖、零历史 baseline、真实租约与 Gateway 重连、共享项目和 worker，以及 `local_admin` 初始化。管理员绑定要求同一控制台会话挑战、真实当前 owner C2C 消息和控制台确认；独立审查修复了与安全存储的锁序冲突，双连接回归通过。健康、指标及受保护的分页恢复点/任务/分片回执/绑定详情复用现有 API/SSE。集成审查发现并修复了诊断状态文件写入失败阻断关键停机清理的问题；3 项真实租约/网关/数据库关闭故障测试通过（8.055 秒），普通诊断异常仅产生固定脱敏日志，取消仍保留原语义。

下载候选在 42 项协议/链接/启动测试及 3 项真实 PostgreSQL + TLS + M3 组合测试中通过；实际 target 验收记录和 HTTPS 回环证明缺一不可，配置成功不代表用户可下载。原链接保留原 token 和期限，不因探测暂失而重签。App 与服务的真实公网反代/目标验收仍待完成，没有自动部署或开启公开服务。

原生文件审计明确：此前正式 M6 只接通 ≤3,500,000 字节的小文件路径，纯协议分片实现尚未连到持久 dispatcher，不满足“大文件分片中断”的完整验收。该阶段开始补受保护 prepare-session 存储、分阶段持久回执、可信 PUT transport 和明确目标分片证据，后续整合及验证结果记在下文；不会把平台文档的 100 MB 直接当作本账号可用上限。

首轮 M8 组合运行 97 项、83.170 秒，发现 3 个测试组合问题（StoreRow 与 tuple 断言、桌面测试导入路径、SDK 隔离断言受既往 OneBot 导入影响），实际功能断言其余通过；修正后的第二轮组合 117 项中 112 成功、3 项 Windows 用例在 macOS 跳过、2 项停机测试夹具因共享池被上一用例真实关闭而失败；改为每用例独立数据库后 3 项停机测试通过。另 12 项实际 catalog 内容/旧数据往返和受保护恢复测试通过（14.737 秒）；此前 `tuple(StoreRow)` 只比较列名的薄弱断言已在独立快照修正，正式 M7 同步修正后也已通过。两个前端 production build 与 7 项 DTO 测试通过，Nix flake 求值包含新增的 23 项模块断言检查。新链接有效期配置（60–3600 秒）已沿可信启动、FileServiceConfig 和实际 dispatcher 接通；49 项配置/真实 PostgreSQL/TLS/文件服务回归通过（44.814 秒），重建 dispatcher 使用更长期限也不改原 publication。Nix flake 求值已接入 24 项模块断言并通过。该阶段的 Windows 原生 ACL CI、剩余 App 增量、原生分片、最终全量及 M9 不变量验收尚未完成；后续结果分别记录，不把当时的定向通过写成生产可用。

原生分片与 App 导入增量现已合入 M8 独立组合候选。42 项协议/旧 uploader、11 项真实 PG/TLS/FakePlatform 测试通过；统一 0068 后，原生分片、链接期限、配置和真实迁移共 **106 项通过**（60.725 秒）。新增降级测试确认旧小文件回执可往返、无 session 的 unknown prepare 不可丢弃、已过期 session 仍保留。App 的 19 项真实 InstanceStore→受保护文件→HostConfiguration 回归及桌面 production build 通过，原 `max_bytes`/身份/版本/时间原样保留。目标证明与上传主机配置互相独立，不由旧小文件证明推导出分片能力。

再次独立审查发现显式 env 与 Settings/路径的进程环境读取不一致，以及控制台密钥缺失导致构造失败时真实数据库池未关闭。现已修复：共享配置、路径和模型 secret 间接引用均从同一映射读取，不临时修改全局环境；两个真实宿主都在原事件循环内等待构造失败的资源清理，成功时移交原 AppContext.shutdown。59 项兼容回归通过，补充构造取消及真实 SDK 阻断生命周期后共 61 个不同用例通过；同步直接 builder 仅能保证同步资源关闭，完整异步回滚由生产宿主的 owned wrapper 提供。另修复可选 bridge 构造失败后遗留未接管 Matrix 客户端，保持可选服务失败仍可启动的原行为，2 项新回归及 8 项构造回归通过。

根线程组合首轮 44 项仅一项旧 parser 测试未通过：它仍把空 `onebot_accounts` 视为非法，与官方单平台的新要求冲突。改为保留对非数组值的拒绝断言，并由现有空来源启动测试证明不伪造 OneBot 账号；相关 17 项通过（5.034 秒）。最后实际启动、构造清理、停止故障、宿主配置与三形态组合 **37 项通过**（36.031 秒）。

新增两项真实 PostgreSQL/FakePlatform 启动测试（6.604 秒）覆盖官方关闭及双平台：实际 OneBotRuntime builder/assembly 与官方 runtime 共用 ledger/journal/pool，官方就绪不依赖 OneBot 连接，两入口入账与各自发送回执隔离，重复启动不增加 worker/健康 writer，停止官方后才关闭共享存储。加上 SDK 阻断下的官方单平台生命周期，三种启动形态均已有具体组合证据；不据此宣称真实 NapCat/腾讯生产连接已验收。

操作说明已同步 App/Nix 私有目录差异、向导实际输出、两个 host 配置入口兼容规则及 multipart/download 精确字段。三份空路线示例通过实际解析器，7 项无网络检查通过。当批 Ruff、compileall 与候选差异秘密扫描通过；最终全量结果见下文，当前提交的 CI 仍待完成。


实际 Chrome 156 浏览器交互另补充验证 M8.3：使用当前 31 个前端源/包文件的独立副本及既有 Playwright，真实挂载 App/useControlPlane/OfficialQQView/OfficialDetails，对本机合成 API/SSE 注入一次官方资源变更。12 项检查通过；原生下拉框展开、焦点、DOM、所选类别、第二页游标和绑定草稿全部保留，队列及执行明细更新，刷新只请求官方状态与当前明细页，没有自动确认绑定。运行后 31 个源文件哈希一致，浏览器及本机服务关闭。这是当前桌面浏览器的交互证据，不代表真实管理员绑定或所有浏览器验收。


M8 首次冻结全量于 2026-10-29 完成 **5,416 项**（3,436.354 秒）：5,254 项成功、22 项跳过、138 项错误及 2 项失败；全部 1,484 个冻结文件哈希保持一致，此次不记为通过。135 项错误来自共享项目夹具仍 patch 已移至 legacy 分支局部导入的 `get_bots`；两项缺少新项目 grant TTL 配置；一项旧 runtime 夹具缺少完整 LoadedOfficialConfig，在到达网关 ready 前失败。另两项失败为 controller Docker 复制清单遗漏新标准库依赖，以及旧断言把空 OneBot 来源的官方单平台构造视为非法。

当前最小修正保持产品授权边界和旧快照：测试改用真实 `nonebot.get_bots` seam 并保留官方路径不调用它的哨兵；补齐类型化运行配置和原 600 秒 grant 策略；空来源测试改为验证真实控制台拒绝未配置旧账号，且不会借用另一服务的来源或改写路由/迁移记录。Controller 的三条精确 COPY 补齐九个标准库模块，原隔离导入断言不变，23 项定向检查通过。

随后独立 `revalidated/source` 快照的定向回归完成 **148 项**（254.574 秒），仍有 **4 项错误**，运行后冻结文件没有漂移。两项启动/迁移服务夹具缺少 `handlers.triggers`，另外两项官方项目前台夹具缺少 `loaded.delivery`。该批次不记为通过，也不代替完整回归。随后仅补齐四方法约束的维护服务夹具和真实 DeliveryConfiguration；原启动顺序、零后台启动、项目权限/撤权和交付断言保持不变。四个出错用例加九项 OneBot 快照共 **13 项通过**（69.017 秒），所有冻结文件哈希一致；Ruff、compileall 通过。该定向批次不代替完整回归；首次全量及定向失败记录继续保留。

修正后重新冻结并运行完整回归，最终 **5,416 项 / 3,515.186 秒，零错误、零失败，22 项跳过**，整体通过。运行结束时全部 **1,484 个冻结文件哈希一致**；原九项 OneBot 出站快照通过且未改期待值。完整日志 SHA-256 为 `d8e6a74b25c4137d584e824cd0268e8b894668e0ba3c61dc8666d2417325bf96`，冻结清单 SHA-256 为 `2717f9643557fc27ac1b7ed2040d9497084929278bf9c7a25efa17b153a1c87e`。全量结束后两个本机隔离数据库的 upgrade/check 均成功，版本为 `0068_qq_official_operations`；Ruff、compileall 及 145 项变更的 Gitleaks 检查通过，秘密扫描零发现。

全量结束后的差异空白检查发现 `sender.py` 一处行尾空格及三个文件多余的结尾空行，清理后逐文件比较完整 Python AST（含位置信息）不变；除结果文档回填外，没有语义修改。

既有 admin-ui、desktop 的 production build 及本机 aarch64-darwin Nix flake 求值证据继续有效，包含 24 项 NixOS 模块断言；本机检查明确省略不兼容系统，不能替代 Linux Nix package CI 或 Windows 原生 ACL。已将验证后的 M8 变更精确整合至正式开发分支，并回填本次结果文档；**M8 本地完整验证通过，PR #9 最终 head 的 Python 3.12、Windows credential ACL、Nix package、Secret scan 四项 CI 全部通过，已以 merge commit `d20fd84586efd0ef0929d34d721971bf2c1519c6` 合并，尚未部署**。本地 22 项跳过不算 Windows 原生 ACL 通过；实际 Windows 结果另记如下。真实公网反代、大文件、部署和历史迁移仍保持原验收边界。

M8 的 [PR #9](https://github.com/zty20040403/gaojibot/pull/9) 最终 head 为 `5f58535f2dcdb7dd4daea746b208756973998884`。此前 Windows 原生 CI 的受限 ACL 夹具曾失败；诊断确认实际 API 与 owner 校验正常，但夹具残留非受信主体的显式 allow，产品拒绝正确。最终只在测试初始化先 `icacls /reset`，再执行原继承限制与当前测试用户授权；产品权限策略及原正负断言保持不变。该 head 的 [Windows 原生 CI](https://github.com/zty20040403/gaojibot/actions/runs/40947626685/job/123707319059) 在 Windows Server 2025 / Python 3.12.10 上运行 **13 项 / 0.164 秒，零错误、零失败、零跳过**，已通过（run `40947626685`，job `123707319059`）。同一 head 的 Python 3.12、Nix package 与 Secret scan 也全部通过；PR #9 已以 merge commit `d20fd84586efd0ef0929d34d721971bf2c1519c6` 合并，正式分支已 fast-forward，文件树与最终 head 一致。此结果单独证明 Windows 原生凭据测试，不把本地 5,416 项或以往失败批次改记为 Windows 通过，也不接通仍受限制的 Windows CLI 报告写入。

## M9：全面回归与交付（本地离线实现验证完成，正式 PR 流程待完成）

独立探测工具新增图片、PDF、ZIP 三个显式小文件检查，使用固定无害样本、逐项新窗口和原始截止时间。上传与 Type 7 消息各自记录阶段和样本哈希；结果不明停止目标，不重新上传、换序号或悄悄改链接。新增的 HTTP header 前检查在连接/TLS 之后复核窗口、媒体 TTL 和批次期限；深层 JSON 被归类为安全的未知响应，取消保持向上传播。此检查为 opt-in，未改变产品默认请求语义。协议库没有引入应用/OneBot 依赖。

新增一份先前获准 M4 事件经 M0 探测器离线脱敏生成的 GROUP_MESSAGE_CREATE 夹具，保留结构化 is_you 与消息字段，真实身份、原文与原时间不进入仓库。它不是本轮新连接或新发送的证据。协议、原生 codec/uploader 和探测定向组合共 188 项运行成功（2.853 秒，5 项平台跳过）；新事件夹具及既有事件解析 13 项通过。M8 本地完整回归及最终 head 的四项 CI 结果已在上节记录；上述定向通过与本阶段完整回归分别记录。

M9 最终冻结完整回归已通过：**5,437 项 / 3,982.899 秒，22 项跳过，零错误、零失败**。运行结束时 **1,505 个冻结文件（1,490 个源文件与 15 个既有前端构建文件）均无漂移**；旧 OneBot 出站快照保持原期待值。完整日志 SHA-256 为 `1ecc4b40b0a63cf39f7be1a45993afd1495ecfde5d076c6d6a0c71798a4fb2c1`，冻结清单 SHA-256 为 `8214385784af078e350c90fb492fe857a268f09fc760366596ce35d1330bf1b0`。Ruff、compileall 均 exit 0；末次两个本机隔离数据库 upgrade/check 均 exit 0，版本保持 `0068_qq_official_operations`，M9 没有新增迁移。本阶段未修改 Nix 或前端源，沿用 M8 已通过的构建/检查证据，不把它们改写为尚未运行的 M9 CI。

11 项 M9 变更已基于 M8 merge `d20fd84586efd0ef0929d34d721971bf2c1519c6` 按候选哈希精确采用到正式开发分支；回归结束后回填这份结果文档与方案，另清理事件测试及其夹具说明各一个末尾空行；Python AST（含位置）保持一致。以上证明 **M9 本地离线实现验证完成，正式 PR 流程仍待完成**。本节为 M9 提交前的本地验证记录：记录时正式 PR 尚未创建，当前提交的 CI、审阅与合并流程尚未完成。之后的 PR 编号、当前 head 检查和最终合并状态以 GitHub 对应 PR 为准；本文不预先宣称其通过。 本阶段没有连接真实 QQ、部署目标服务、迁移实际历史或扩大测试范围；§13 的 PR/交付及受控上线门槛不能由本地通过代替。

探测说明整理了剩余受控验收、固定参数与报告边界。CLI 未提供的分片、撤回、发语音契约继续明确标记；Windows 受保护探测报告仍限制使用，不能把凭据读取 ACL 的支持推导为所有 Windows 工具可用。完整待验清单见[剩余受控验收与记录](qq-official-probe.md#剩余受控验收与记录)，现有真实证据限于此前获准群的文字、帮助命令和小 PDF/ZIP 下载打开。未获下载副本哈希，真实大文件、公网下载、管理员 C2C、历史迁移/回退与生产启用仍待验收。

### 第 5 节不变量测试索引（M9）

下表为测试映射，不将入口存在等同于本次通过；完整断言、补充用例和真实环境限制见[不变量文档](qq-official-invariants.md)。M7 CI、M8 本地全量及最终 head 的 Windows 原生 ACL CI 以本记录相应批次为准；真实大文件验收仍待结果。

| # | 不变量 | 代表测试（类.方法） |
|---|---|---|
| 1 | 旧路径隔离 | [OneBotPlatformSnapshots.test_ordinary_group_reply](../tests/test_onebot_platform_snapshots.py) |
| 2 | 入站可靠接管 | [OfficialInboxTests.test_checkpoint_update_failure_rolls_back_receipt_dedup_inbox_and_job](../tests/test_qq_official_inbox.py) |
| 3 | 发送结果不明不重放 | [OfficialSenderTests.test_dropped_response_is_ambiguous_and_never_replayed](../tests/test_qq_official_sender.py) |
| 4 | 窗口原子分配 | [OfficialSendPolicyTests.test_concurrent_window_claims_do_not_overspend_or_duplicate_sequences](../tests/test_qq_official_send_policy.py) |
| 5 | 配额不因重启清零 | [OfficialSendPolicyTests.test_unverified_daily_limit_never_refills_on_restart_or_elapsed_time](../tests/test_qq_official_send_policy.py) |
| 6 | 等待有类别和期限 | [PlanTests.test_normal_purposes_obey_default_wait_limit_and_preserve_absolute_deadline](../tests/test_qq_official_plans.py) |
| 7 | 身份与数据隔离 | [PlatformIdentityStorageTests.test_same_opaque_id_isolated_by_app_environment_kind_and_namespace](../tests/test_platform_identity_storage.py) |
| 8 | 未绑定群不消费业务 | [OfficialInboxTests.test_unbound_ordinary_message_persists_no_body_nickname_url_or_content_hash](../tests/test_qq_official_inbox.py) |
| 9 | 首次身份可信 | [OfficialBindingTests.test_first_contact_does_not_enroll_owner_and_config_is_idempotent](../tests/test_qq_official_binding.py) |
| 10 | 凭证不外泄 | [OfficialReceiptTests.test_otp_code_never_enters_receipts_ledger_manifest_or_public_hash](../tests/test_qq_official_receipts.py) |
| 11 | 能力与授权分开 | [PlatformToolGatesTests.test_all_feature_gates_apply_to_definition_and_execution](../tests/test_platform_tool_gates.py) |
| 12 | @ 和引用不可伪造 | [RendererTests.test_same_literal_member_different_app_env_or_group_not_authority](../tests/test_qq_official_renderer.py) |
| 13 | 单实例有边界 | [LeaseTests.test_same_account_in_another_schema_still_competes](../tests/test_qq_official_lease.py) |
| 14 | 项目访问须证明 | [ProjectAuthorityTests.test_actual_task_binding_freezes_original_grant_revision](../tests/test_qq_official_project_authority.py) |
| 15 | 任务完成不等于交付完成 | [OfficialFileReportingTests.test_accepted_message_does_not_claim_download_or_pass_failed_content](../tests/test_qq_official_file_reporting.py) |
| 16 | 迁移与回退可审计 | [JournalTests.test_rollback_revokes_grants_and_old_epochs_while_preserving_receipts](../tests/test_qq_official_migration_journal.py) |
| 17 | 文件清理与有效期一致 | [IntegrationTests.test_link_expired_then_artifact_gc_keeps_audit_and_no_live_bytes](../tests/test_qq_official_file_links_integration.py) |

### 三种启动形态的证据矩阵

默认配置保留 OneBot、关闭官方入口；启用官方单平台或双平台必须显式选择。下表区分完整回归形态和定向证据，批次结果、CI 状态与执行环境均以[进度记录](qq-official-progress.md)为准。

| 启动形态 | 具体证据 | 覆盖范围与边界 |
| --- | --- | --- |
| 默认官方关闭、OneBot 启用 | [PlatformModeTests.test_default_and_dual_preserve_onebot](../tests/test_official_application_startup.py)；[DualApplicationTests.test_real_legacy_only_context_never_opens_official_credentials](../tests/test_dual_application_postgres.py) | 默认开关及实际旧平台 builder、assembly、worker、ingest、关闭；官方凭据读取哨兵从未触发。该形态的完整兼容回归结果单独记录，不以这两条定向测试代替全量。 |
| 官方单平台（SDK-blocked 验证） | [PlatformModeTests.test_full_product_asgi_import_has_no_nonebot](../tests/test_official_application_startup.py)；[NativeApplicationTests.test_real_epoch_zero_legacy_baselines_projects_security_reconnect_shutdown](../tests/test_official_application_postgres.py) | 禁止导入 NoneBot 的独立进程检查真实产品入口；实际 PG/Fake 生命周期检查零 legacy baseline、项目/安全服务、命令、重连、健康与关闭。实际生命周期的 SDK-blocked 执行批次见进度记录；普通运行该方法本身不自动安装导入禁令。 |
| 双平台同时启用 | [DualApplicationTests.test_real_dual_context_owns_one_worker_set_two_ingresses_and_ordered_shutdown](../tests/test_dual_application_postgres.py) | 真实 OneBotRuntime/OfficialRuntime 共用 context、ledger、journal 和 pool；无 OneBot 连接时官方仍 ready，两端分别入账，官方 `/help` 经 M3 到 committed。并发重复 start 不复制共享 worker/健康 writer；官方先停，再关闭共享存储，fixture 独立 pool 不被误关。实际平台 I/O 为 FakePlatform/假 Bot 边界，未据此声称真实双平台部署通过。 |

## 后续里程碑及门槛

| 里程碑 | 状态 |
| --- | --- |
| M1 回合入口、OneBot 快照、平台身份 | 已合并；3,479 项本地全量通过，PR #2 三项 CI 通过，用户已审阅 |
| M2 持久入站、owner、新群绑定、实例租约 | 已合并；3,626 项全量通过，PR #3 三项 CI 通过，用户已审阅 |
| M3 持久发送计划、窗口、额度、回执 | 已合并；3,952 项全量通过，PR #4 三项 CI 通过；单群单条真实收发已确认 |
| M4 新群/C2C 回合与首批命令 | 已合并；4,139 项全量通过、PR #5 三项 CI 通过，真实 `/help` 用户确认 |
| M6 文件出口和生命周期 | 已合并；4,530 项全量与 PR #6 三项 CI 通过；指定测试群小型 PDF/ZIP 已获发送回执，用户确认下载并打开 |
| M5 项目授权、历史迁移与回退 | 已合并；本地全量 5,039 项通过、PR #7 三项 CI 通过；实际迁移验收待完成 |
| M7 其余业务与可选能力 | 已合并；最终提交 PR #8 的完整 Python、Nix package、Secret scan CI 通过，最终本地 83 项定向回归通过；可选平台能力按证据保持关闭 |
| M8 配置、App、控制台、指标、文档 | 已合并（`d20fd84586efd0ef0929d34d721971bf2c1519c6`）；本地完整回归 5,416 项、22 跳过、零错误/失败；PR #9 最终 head 四项 CI 全通过，Windows 原生 13 项零跳过；未部署 |
| M9 全面回归与交付 | 本地离线实现验证完成：5,437 项、22 跳过、零错误/失败，1,505 文件无漂移，11 项变更已整合正式开发分支；本记录提交时 PR 尚未创建，CI/合并流程待完成，后续状态以 GitHub 对应 PR 为准 |

按方案每个里程碑分别提交 PR；按用户最新授权，由执行者审阅实现、测试和 CI，合适后用 merge commit 合并。前一个 PR 未合并时，下一阶段可本地推进，但不把后续提交推入前一个 PR。已完成上述单群单条纯文本收发验证；其他目标能力、下载副本哈希、迁移/回退与生产切换尚未验收。
