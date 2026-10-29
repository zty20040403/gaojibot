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

## M4：共享回合、首批命令与文字后台任务（离线完成，PR 待验证）

- 在既有持久 job 上增加执行记录，与 journal、结果和发送计划关联；没有第二套模型循环。`prepared` 才能启动执行，持久化后的结果可恢复交接；执行中失锁或进程中断进入需核对状态，不自动重跑不确定的模型或工具操作。
- 迁移 `0064_qq_official_turn_execution` 保存规范化 `turn_text` 及版本。TxA 按真实结构化 `is_you` 证据剥离机器人前缀，TxB 与账本一同提交文本；旧 NULL 输入不从原始 payload 重建。绑定码和身份控制命令不进入普通模型上下文。
- 账号绑定的 TurnPlatform 校验原始账号、端点、actor、路由与租约；最终文本只交给 M3。默认关闭流式发送、进度、文件和主动/新窗口探测。当前连接、凭证、绑定、准入、发送机会分别判断。
- `/bind`、帮助、模型、effort、本人用量、本人任务查询与取消复用既有控制入口和共享命令分发。个人设置按实际 endpoint/actor 隔离，群默认只允许当前本地 owner 修改。其余命令明确告知暂不支持。受保护 whoami 发送/审计保留原文，普通账本采用明确脱敏占位并复用 command 排除机制，避免后续模型摘要带出身份。
- 复用现有 SubAgentCoordinator、任务/步骤/会话和检查点，官方任务使用结构化账号/端点/actor；只开放文字研究、分析和只读上下文工具。独立官方 worker 不需要 OneBot Bot 实例，结果经 M3 持久发送；取消、恢复和命令边界仍复查真实任务所有者与当前执行租约。
- 每次模型物理请求及回退前检查 job/account 租约；受保护执行禁用 SDK 隐式重试。共享接口保留未传入官方生命周期时的旧行为。

当前已经通过的定向验证包括：真实本机 FakePlatform 群 @ 和 C2C → 共享模型工具回合 → 正确 Markdown/msg_id/msg_seq → 持久 HTTP 回执；命令经真实发送器交付；超过原 5 分钟窗口的实际 Sub-Agent，在测试显式开放的主动策略下发送，拒绝后等待真实新消息窗口再发送；模型请求边界、取消、PG backend 丢失、事务回滚和冻结结果恢复。最终完整回归和真实帮助命令验证见下文；模型及后台任务的真机验证、生产部署仍未完成。

用户授权的同一测试群已完成真实 `/help` 验证：实际 `GROUP_MESSAGE_CREATE` 含 `mentions.is_you=true`，经生产 codec、TxA/TxB、共享命令路由及 M3 发送器处理，恰好一条 Markdown 帮助回复取得真实回执并提交账本，用户确认看到了该回复。测试没有模型调用、文件上传或 owner 授权，结束后关闭连接并释放租约。第一次临时测试脚本遗漏允许合法的 `message_reference` 字段，在 HTTP 发出前拦截；保留原始不确定记录，没有重放。修正脚本并通过 4 项真实 PG→M3 载荷回归后，使用用户新发的消息和独立 schema 完成上述验证。

第一次 M4 全量运行 4,139 项（1,414.058 秒，19 项跳过），仅发现一处旧 fleet 测试的兼容错误：旧任务对象没有新增的 `endpoint` 属性。修复采用兼容读取，不修改旧测试期望；相关 100 项测试通过。随后重新冻结 64 个实现和测试文件，最终全量 **4,139 项测试通过**（1,439.072 秒，19 项跳过），结束后全部哈希一致。完整 ruff、compileall、空白检查通过；两个隔离库的 schema upgrade/check、Nix flake 求值检查及 staged patch 的 Gitleaks 扫描通过。fresh fetch 的 main 与 M3 合并基线一致。M4 已达到本阶段离线完成标准，当前 PR 的 CI 和合并状态另行记录。

## 后续里程碑及门槛

| 里程碑 | 状态 |
| --- | --- |
| M1 回合入口、OneBot 快照、平台身份 | 已合并；3,479 项本地全量通过，PR #2 三项 CI 通过，用户已审阅 |
| M2 持久入站、owner、新群绑定、实例租约 | 已合并；3,626 项全量通过，PR #3 三项 CI 通过，用户已审阅 |
| M3 持久发送计划、窗口、额度、回执 | 已合并；3,952 项全量通过，PR #4 三项 CI 通过；单群单条真实收发已确认 |
| M4 新群/C2C 回合与首批命令 | 4,139 项全量通过；真实 `/help` 用户确认，PR 待验证 |
| M6 文件出口和生命周期 | 未实现；须先于 M5 |
| M5 项目授权、历史迁移与回退 | 未实现 |
| M7 其余业务与可选能力 | 未实现 |
| M8 配置、App、控制台、指标、文档 | 未完成；M2 所需配置与 App 启停已提前接线，部署/健康/ACL 验收待完成 |
| M9 全面回归与交付 | 未完成 |

按方案每个里程碑分别提交 PR；按用户最新授权，由执行者审阅实现、测试和 CI，合适后用 merge commit 合并。前一个 PR 未合并时，下一阶段可本地推进，但不把后续提交推入前一个 PR。已完成上述单群单条纯文本收发验证；其他目标能力、文件领取、迁移/回退与生产切换尚未验收。
