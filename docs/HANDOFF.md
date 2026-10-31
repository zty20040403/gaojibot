# gaojibot 改造交接文件

## 当前执行记录（2026-10-31）

- W1–W7、W9–W10 的实现已合并；最终测试及统一部署正在进行，生产切换尚未完成。
- W8 GitHub 机器人功能按 owner 要求暂缓，不等待新账号/token。
- PR #31–#51 覆盖窗口和记忆、管理台清理、后台任务、定时任务、只读运维、VM 浏览器/PDF/视频、语音、消息合并、主动接话和旧代码清理。
- 新迁移到 `0077`。保留聊天、绑定、文件、工作区与仍供 cluster-control 使用的 Fleet 数据；删除 104 张废弃表前必须备份。
- 本次会话已授权自行审阅、使用 merge commit 合并，并在全部完成后统一部署。数据库保持现有主备方向，不在开发阶段切换生产版本。
- 当前运行说明以 README、qq-official-host-config 和 deploy-rework 为准。下面是最初的任务交接快照，旧实现位置和未完成标记不代表最新状态。


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
| 读网页在 VM 里 | `webread <url>` 命令：trafilatura 抽正文，pdftotext 读 PDF，`--offset` 续读；VM 的网络本来就进不了内网 | 我们的 `web_fetch` 在宿主上跑，已经限制只能访问公网。再加一个 VM 里的 `webread`，补上 PDF |
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
- VM 网络本来就隔离、进不了内网，所以只能由宿主来调用，**不要把 ssh key 或 token 放进 VM**。
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
