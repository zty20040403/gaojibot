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

## M1：共享回合与身份隔离（离线完成，待 CI 与审阅）

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

本阶段达到离线完成标准，GitHub CI 与用户审阅另行记录。`Delivery.target_scope` 仍是旧三字段表示，官方持久投递在 M3 补齐前不得接通；项目/宿主授权、媒体和剩余业务仍按 M4/M5/M6/M7 分步实现。M1 不构成上线或真实 QQ 能力已验证的声明。

## 后续里程碑及门槛

| 里程碑 | 状态 |
| --- | --- |
| M1 回合入口、OneBot 快照、平台身份 | 离线完成；3,479 项全量通过，待 CI 与用户审阅 |
| M2 持久入站、owner、新群绑定、实例租约 | 未实现 |
| M3 持久发送计划、窗口、额度、回执 | 未实现 |
| M4 新群/C2C 回合与首批命令 | 未实现 |
| M6 文件出口和生命周期 | 未实现；须先于 M5 |
| M5 项目授权、历史迁移与回退 | 未实现 |
| M7 其余业务与可选能力 | 未实现 |
| M8 配置、App、控制台、指标、文档 | 未实现 |
| M9 全面回归与交付 | 未完成 |

按方案每个里程碑分别提交 PR；用户审阅并用 merge commit 合并。前一个 PR 未合并时，下一阶段可本地推进，但不把后续提交推入前一个 PR。真实账号/测试群报告尚未提供；目标能力、文件领取、迁移/回退与生产切换均未验收。
