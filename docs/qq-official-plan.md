# QQ 官方机器人接入方案（qq-official）

> 状态：执行中，2026-10-29。M0–M4 已通过本地/CI 并合并；M4 merge commit 为 `606b01660b139bd182c6d1623016827fc879f01b`，PR #5 三项 CI 通过、本地全量 4,139 项。用户授权的唯一测试群已确认真实纯文本及 `/help` Markdown 回复可见。M6 文件出口已完成离线实现，全量 4,530 项通过，待 PR CI；真实文件领取仍待验收。M5 仅在仓库外准备隔离草稿，尚未执行迁移。具体结果见 [实施进度](qq-official-progress.md)。下文“本次/本轮只修订文档”描述原方案修订轮，不代表当前实施进度。
> 读者：负责实现的 AI 执行者，以及审阅它的人。
> 一句话：让 gaojibot 直接用 WebSocket 连接腾讯 QQ 开放平台，按腾讯的格式收发消息，不经过 NapCat 和 OneBot。现有 NapCat 路径保持不变，两条路径并存。

---

## 0. 给执行者的规则（先读完再动手）

### 0.1 标记含义

- **【必须】**：硬性要求，不能改。做不到就停下来，写明原因，等人决定。
- **【自行判断】**：给了建议和约束，具体做法由你决定。决定后在提交说明里写一句理由。
- **【页面已核对】**：已经看到当前账号后台的配置或说明，不等于该接口调用已经成功。
- **【文档已核对】**：腾讯文档描述了这个能力；仍需核实当前账号、目标群和环境是否开放。
- **【待实测】**：真实接口行为尚未确认。实现成功路径和明确的不可用/降级路径；默认未知，不能虚构接口或自动扩大权限。测试夹具通过不算真机通过。

### 0.2 工作方式

1. 【必须】开始实现时在**单独的 git worktree** 里、`feat/qq-official` 分支上工作。分支、PR 和合并的完整流程见 0.4 节。**不要在 `/Users/kenneth/Desktop/comp390/bot` 里切换分支**：另一个会话可能正在那里的 `main` 上提交。本轮只修订本文件，不创建实现分支、不提交、不部署；本文的后续执行流程不代表本轮已获执行授权。
   除了按 0.4 节向 GitHub 推送 `feat/qq-official` 分支和 `pre-qq-official` 标签，**不连接生产或远程服务器，不使用真实机器人凭证**。本机隔离的假服务和测试数据库允许使用。真实探测写成工具或清单交给用户执行（见 M0 和第 11 节）。
2. 【必须】按第 6 节的依赖顺序执行，M6 文件交付先于 M5 历史迁移。每个里程碑完成后：
   - 小提交运行直接相关测试；里程碑提交前运行全套测试（第 10.1 节），公共接口、身份、数据库或投递变更额外覆盖关联路径；
   - 提交（较大的里程碑可以拆成多个提交），提交说明写清做了什么、哪些【自行判断】做了什么选择；
   - 在 `docs/qq-official-progress.md` 追加一段进度记录：完成项、偏离方案的地方、遗留问题；
   - 按 0.4 节推送并开 PR。
3. 【必须】未迁移的 NapCat/OneBot 群行为不变。只有管理员明确执行会话迁移后，指定群才切换入口；不能为接官方渠道顺便修改其他 OneBot 行为。M1 有专门回归手段。
4. 【必须】默认关闭：`AI_QQ_OFFICIAL_ENABLED` 不打开时，不启动官方连接、运行时任务或业务写入。显式 Alembic 升级可以新增空表；迁移后的端点恢复走第 12 节，关闭开关不能偷偷把待投递文件改发到旧群。
5. 【必须】另一个会话可能同时在这个仓库里工作。开始前先 `git status`；只改本方案涉及的文件；数据库迁移编号在**提交前**再确认一次（见第 7 节），保证 Alembic 只有一个 head。
6. 【必须】不要把真实的 AppID、AppSecret、openid、群号、QQ 号写进仓库，包括测试和文档。测试数据一律用明显的假值（如 `GROUP_OPENID_TEST_1`）。
7. 遇到本方案没覆盖、又不属于【自行判断】的问题：先选最保守、可回退的做法，标注 `TODO(qq-official)`，并在进度记录里列出来。

### 0.3 当前账号的事实与本次修订

2026-10-28，通过用户已登录的 QQ 开放平台后台只读核对。此处不保存真实 AppID、QQ 号、姓名或密钥。

| 项目 | 页面实际状态 | 实现含义 |
|---|---|---|
| 主体 | 已认证（个人） | 不再套用未认证账号的服务范围和额度 |
| 管理员 | 当前用户拥有该机器人全部管理权限 | 首次证明聊天身份后长期识别 owner；不反复绑定 |
| 公开好友/私聊 | 可设置，目前关闭 | 腾讯服务范围与本地允许名单分开；不写死仅体验用户 |
| 任意群聊公开使用 | 可设置，目前关闭；个人认证说明上限 500 群 | 不写死只能进入创建者管理的群；开放前仍须用户决定 |
| 普通群可申请能力 | 已选撤回、进群管理、发言管理；未选移除成员 | 是可申请项，不是所有群的有效授权 |
| 白名单群可申请能力 | 四项均选，白名单群数量为 0 | 这里是开发者配置的群名单，不等于腾讯特殊接口审批白名单 |
| 群内实际授权 | 页面明确要求群主单独授权 | 按目标群核对，不能由平台管理员身份推导 |
| 接入与连接 | WebSocket；当前离线、未连接服务 | 可继续此接入方式，不自动改成 Webhook |

本次只调整设计：身份安全、公开服务、可靠投递、文件保留、迁移及回退。不会替用户打开公开服务、扩大群权限、重置密钥或使用聊天里出现过的凭证。

### 0.4 分支、PR 和合并流程

目标有三个：现在的 `main` 不受影响；改造前的版本永久保留；改动通过 PR 合进 `main`，计入用户的 GitHub contribution。

**一次性准备**（开始 M0 之前）：

1. 检查提交身份：worktree 和主目录共用仓库配置。确认提交邮箱属于用户且已关联其 GitHub 账号；和上一笔邮箱一致并不能单独证明贡献归属。不要修改全局 git 配置。贡献统计还受默认分支、仓库类型和 GitHub 规则影响，不承诺仅靠合并方式就一定显示。
2. 给当前的 `main` 打标签并推送，永久保留改造前的版本：
   ```bash
   cd /Users/kenneth/Desktop/comp390/bot
   git fetch origin
   git tag pre-qq-official origin/main
   git push origin pre-qq-official
   ```
   标签已经存在时不要覆盖，停下来问用户。
3. 建 worktree 和分支：
   ```bash
   git -C /Users/kenneth/Desktop/comp390/bot worktree add /Users/kenneth/Desktop/comp390/bot-qq-official -b feat/qq-official origin/main
   ```
4. 把本方案文件放进分支作为第一个提交（主目录里这个文件还没被 git 跟踪，新 worktree 里没有，需要复制过去），提交说明如 `docs: add QQ official bot integration plan`。

**每个里程碑的循环**：

1. 本地完成这个里程碑，全套测试通过，进度记录写好。
2. **同步 main**：`git fetch origin && git merge origin/main`。【必须】用 merge，不用 rebase（分支已经推送过，rebase 会改写历史）。有冲突就解决；解决后重新跑全套测试，并重新确认数据库迁移编号，保证 Alembic 只有一个 head（第 7 节）。
3. **推送**：`git push -u origin feat/qq-official`。【必须】不许 `--force`，不许推送到 `main`。
4. **开 PR**，目标分支 `main`，标题如 `qq-official M0: 协议库、假服务器和探测工具`。PR 描述包含：
   - 这个里程碑做了什么；
   - 新增和修改的测试，以及本地测试结果；
   - 涉及第 5 节的哪几条不变量，分别由哪些测试保证；
   - 这一步里【自行判断】做了哪些选择；
   - 「`AI_QQ_OFFICIAL_ENABLED` 默认关闭，合并后现有部署行为不变」的说明，以及 OneBot 出站快照测试的结果（M1 起）。

   实施时检查可用的 GitHub 工具和登录账号，不预设 `gh` 是否已安装。使用用户授权的账号开 PR；没有可用工具就交付比较链接 `https://github.com/zty20040403/gaojibot/compare/main...feat/qq-official?expand=1` 和标题、描述。PR 活动归属与 commit 作者归属分别核对。
5. **等 CI 通过**（仓库的 CI 会对 PR 自动运行）。CI 失败就在分支上修，再推送。
6. **审阅后合并**，合并方式选 **「Create a merge commit」**，**不要选 Squash 或 Rebase**，保留每个提交及原始作者信息。用户于 M3 审阅环节明确授权“以后自己审阅，合适就合并”；此后由执行者核对里程碑范围、实现审查、全部必要测试和当前 PR head 的 CI，满足条件后自行合并，无须逐次请求授权。此授权不包含生产部署或扩大真实测试目标。
7. 合并后，先 `git fetch origin && git merge origin/main`，再开始下一个里程碑。

**补充规则**：

- 一个里程碑开一个 PR。M1 太大的话可以拆成几个 PR，但每个 PR 都必须能单独合并：测试全绿，功能默认关闭，OneBot 行为不变。
- 上一个 PR 还没合并时，可以在本地继续做下一个里程碑，但**先不要推送**，等上一个 PR 合并后再推送，避免新提交混进还没合并的 PR。
- 每个 PR 合并进 `main` 后，现有部署的行为都必须不变。这一点由不变量 1 和 M1 的出站快照测试保证，所以可以放心分批合并。
- 任何时候需要看改造前的版本，就看 `pre-qq-official` 标签。

### 0.5 参考资料

- 腾讯 Node SDK：<https://github.com/tencent-connect/qqbot-nodejs>（MIT）。作为实现参考，不凌驾于接口文档或当前账号的实测；实施时记录参考 commit，特别关注 `routes.ts`、`media.ts`、`media-chunked.ts`、`retry.ts`、`gateway/`。
- 腾讯官方 OpenClaw 插件：<https://github.com/tencent-connect/openclaw-qqbot>（README 里有能力和限额说明）。
- QQ 开放平台文档：<https://bot.q.qq.com/wiki/develop/api-v2/>（消息收发概述、富媒体、事件、网关）。
- 同类项目的设计和实测记录：Hank 的 max，`maxx` 分支 `docs/rust-rewrite.md` 的「QQ 官方机器人」和「身份」两节：<https://github.com/HCHogan/max/blob/maxx/docs/rust-rewrite.md>（MIT）。**只参考设计和实测结论，不照抄代码**；引用实测结论时在代码注释或文档里注明出处。

事实依据优先记录为「当前账号的受控实测 / 当前后台配置 / 腾讯接口文档 / SDK 版本 / 第三方记录」，不是一条简单的覆盖规则：后台开关不能证明 API 成功，其他账号的成功不能证明本账号有权限。冲突时保留来源、日期、原始错误码和待验证项。

- [消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)：窗口、主动发送频控、去重。
- [WebSocket](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html)：恢复位置、动态 session 限额和关闭码。
- [访问凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)：当前 token 地址与刷新。
- [全量群消息结构](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)：引用、递归元素、附件字段。
- [PostgreSQL 锁说明](https://www.postgresql.org/docs/current/view-pg-locks.html)：advisory lock 只在同一数据库内互斥。

---

## 1. 背景和目标

### 1.1 为什么做

- NapCat 普通 QQ 登录路径存在设备下线和平台风控风险，已经影响用户体验。
- 官方接入不再需要维持普通 QQ 客户端登录，但仍受内容审核、机器人/开发者账号规则约束，不承诺永久在线或绝不受限。
- 对 App 的产品方向更重要：用户可创建自己的官方机器人，完成必要认证、服务范围和安全配置后接入，不需要准备普通 QQ 小号。
- 腾讯文档提供全量群消息、Markdown 等能力；是否能在目标群使用，按实际授权和验收结果决定。

### 1.2 目标

- 新增平台 `qq-official`，和现有的 `onebot-v11`、`matrix`、`imessage` 并列。
- gaojibot 的核心能力在官方渠道上可用：对话、群聊上下文、Historian、群百科、主动发言、Sub-Agent、例行任务、提醒、项目空间、沙箱、可靠投递。
- 官方渠道没有的能力（群文件、贴表情、合并转发等）按第 9 节的对照表降级，并且**让模型知道**哪些能力没有，不给它对应的工具。

### 1.3 不做

- 不写「把官方接口翻译成 OneBot」的转接层，不把 openid 映射成假 QQ 号。
- 不改编程语言（第 2 节）。
- 不做 QQ 频道（guild），只做群聊和私聊（C2C）。
- v1 不依赖 `union_openid` 或需要另行审批的成员事件，不为未知能力扩大 intents。后台已提供的群管理能力列入 M7 的可选适配范围，逐项核实，不再一概排除。
- v1 不做流式输出和按钮交互，但不能做出阻碍以后加上的设计（见 M7）。

---

## 2. 语言：继续用 Python

【必须】本方案用 Python 实现，不改语言。理由：

1. **规模**：已有大量 Python 业务、测试和数据库迁移。换平台时同时重写语言会放大回归范围，不能把他人的重写进度作为本项目的实施依据。
2. **瓶颈先测量**：模型、网络和数据库等待是主要排查方向；保留 `DatabaseCalls`/`db_call` 边界，不能据此宣称所有同步阻塞已消除。
3. **问题在结构**：现在难接新平台，是因为回合入口直接依赖 OneBot 的数据类型，这是架构问题。用 Rust 照原样重写，问题照旧；在 Python 里解耦（M1）就能解决。
4. **生态**：大模型 SDK、向量检索、OCR、音频处理，Python 的支持最完整。
5. **两件大事不要同时做**：换平台和换语言叠在一起，出了问题分不清是谁的锅。

以后什么时候再考虑：M1 完成后，平台层和核心之间有了清晰的边界。到那时如果确实需要单文件分发（App）或更强的类型保证，可以**一块一块地换**，不用整体重写。`src/qq_official/` 这个纯协议库不依赖 gaojibot 的任何代码，是最适合第一个移植的模块。候选语言：想要简单、单文件部署和好写的并发，选 Go；更看重正确性保证、愿意多花时间，选 Rust。

---

## 3. 官方接口事实表

这一节记录文档约定和待确认项，不宣称全部经过本账号实测。M0 的用户探测结果必须带环境、能力范围和验证时间；不把测试群成功扩大解释成所有群成功。

### 3.1 连接和鉴权（文档与后台已核对）

| 项 | 内容 |
|---|---|
| 凭证 | AppID + AppSecret，在开放平台「开发设置」里获取 |
| 换 token | 文档当前地址 `POST https://api.bot.qq.com/app/getAppAccessToken`，body `{"appId": ..., "clientSecret": ...}`；使用返回的 `expires_in`，不能每次获取旧 token 都擅自续期两小时 |
| 刷新时机 | 文档说明接近到期的 60 秒内可获取新 token，旧 token 在重叠期仍有效。建议提前约 50 秒刷新；single-flight、退避，并处理仍返回旧 token 的情况 |
| REST 鉴权 | 请求头 `Authorization: QQBot {access_token}` |
| REST 域名 | 默认 `https://api.bot.qq.com`；旧 `https://api.sgroup.qq.com` 仅作明确配置的兼容项，不静默切换环境 |
| 沙箱 | 独立配置并核对官方当前沙箱地址；环境进入全部缓存、锁和数据库命名空间。不能把生产凭证发送到任意自定义主机 |
| 错误 | 有的错误返回 HTTP 200，错误码在 body 里（`code` 或 `err_code`），所以**每个响应都要检查 body**，不能只看 HTTP 状态码 |
| 接收事件 | 当前后台是 WebSocket，且有切换接入方式的入口。v1 保持 WebSocket，不修改页面设置；不再声称配过 Webhook 永远无法切回。切换是否影响现有服务需另行授权和验收 |
| IP 白名单 | 后台说明设置后只允许名单内地址。是否必须配置以本账号调用结果为准；识别拒绝并给出实际错误，不把登记公网 IP 写成所有用户的必做步骤 |

### 3.2 网关协议（以官方文档为基线，差异须记录）

- `GET /gateway`（或 `/gateway/bot`）拿到 WebSocket 地址。
- 收到 op 10 Hello，里面有 `heartbeat_interval`。
- 发 op 2 Identify：`{"token": "QQBot {access_token}", "intents": ..., "shard": [0, 1]}`，收到 READY（带 `session_id`）。
- 按间隔发 op 1 Heartbeat（`d` 为最后收到的序号 `s`），收到 op 11 ACK。连续收不到 ACK 就判定连接已死，主动重连。
- 收到 op 0 Dispatch：`t` 是事件名，`s` 是序号，`d` 是数据，`id` 是事件 id。
- 断线后用 op 6 Resume（`token`、`session_id`、`seq`）续上，避免丢事件；op 7 表示服务端要求重连；op 9 表示 session 无效，要重新 Identify。
- 使用 `/gateway/bot` 返回的 `session_start_limit`（remaining、reset_after、max_concurrency）控制 Identify；不写死每天 1500 次。Resume 失败再 Identify，预算耗尽等待重置。
- intents（腾讯 SDK `gateway/constants.ts`）：`GROUP_AND_C2C = 1 << 25`（群聊和私聊）；`INTERACTION = 1 << 26`（按钮，v1 不订阅）。`GROUP_MEMBER_EVENT = 1 << 24` 需要平台审批，有项目报告没批就订阅会被断开（close code 4014），**不要订阅**。
- 网关关闭码与保守处理：SDK 常量名称和官方网页有冲突，不能直接用常量名作为用户诊断。
  - 4004 鉴权失败 → `invalidate()` token，换新 token 后重新 Identify；连续失败就停止重连，健康状态报错；
  - 4006 session 无效、4007 序号越界 → 清理无效恢复点，按预算重新 Identify；
  - 4009 → 官方文档允许 Resume，先尝试，明确失效后才 Identify；
  - 4008 频率限制 → 加大退避后重连；
  - 4900–4913 → 官方表建议重新 Identify；按版本化错误表、退避与预算处理，保留可能存在消息缺口的状态；
  - 4001、4002、4010–4014 → 协议、分片或 intent 配置错误，停止自动紧密重连，报告配置问题；
  - 4914/4915 → 官方网页解释为下架/封禁，SDK 命名却指向 intents。停止重连并显示原码、环境及资料差异，提示检查平台状态，不擅自扩权限、切沙箱或宣称一定可解封；
  - 其他未知关闭码 → 有上限的退避尝试，达到预算后暂停并告警。
- 持久化 session_id 与已经可靠接收的连续序号；不能只在正常退出时存，也不能把内存里刚看到但尚未入队的序号当恢复点。恢复失败且无法补历史时，明确记录缺口，不承诺平台之外的“永不丢消息”。

### 3.3 事件（文档约定，本账号待验收）

| 事件 | 含义 |
|---|---|
| `GROUP_AT_MESSAGE_CREATE` | 群里 @ 机器人的消息（没开全量消息时） |
| `GROUP_MESSAGE_CREATE` | 群主开了「接收群内全部消息」后，**所有**群消息走这个事件，@ 机器人的也走这个；是否 @ 看 `mentions` 里 `is_you: true` 的项 |
| `C2C_MESSAGE_CREATE` | 私聊消息 |
| `GROUP_ADD_ROBOT` / `GROUP_DEL_ROBOT` | 机器人被拉进群 / 移出群 |
| `GROUP_MSG_RECEIVE` / `GROUP_MSG_REJECT` | 群里「机器人主动发言」被打开 / 关闭 |
| `C2C_MSG_RECEIVE` / `C2C_MSG_REJECT` | 用户允许 / 拒绝机器人主动私聊 |
| `INTERACTION_CREATE` | 按钮点击（v1 不处理，只记日志） |

【必须】两种群消息事件都要处理，不能只认其中一种（已有项目因为只处理 `GROUP_AT_MESSAGE_CREATE` 导致开了全量消息后收不到任何消息）。同一条消息理论上不会两种事件都来，但仍按事件 id 和消息 id 去重。

### 3.4 入站消息格式（保留完整结构，容忍字段缺省）

- 区分三类 id：网关 envelope 的事件 `id`、事件体 `d.id` 的消息 id、引用索引 `msg_idx`。`native_message_id` 存消息 id；仅事件回复使用 `event_id`。不能把 envelope.id 当成 msg_id。`msg_idx` 可能来自 `message_scene.ext` 的键值项，按真实结构解析。
- 发送者：群里 `author.member_openid`，私聊 `author.user_openid`；昵称/平台角色按实际字段存在时读取，缺失就留未知，不编造群名片或管理员身份。`author.member_role` 即使出现 owner/admin/member，也只是平台群角色。
- @：正文里写作 `<@member_openid>`，`mentions[]` 里也有，@ 机器人的那一项带 `is_you: true`。
- 引用：`message_type = 103`，解析 `msg_elements` 和 `message_scene.ext` 的 `ref_msg_idx`。官方元素结构允许 author、附件和递归子元素；有就保留，没有才降级。缺少作者不能推断是当前用户。
- 媒体：解析 `attachments[]` 的 URL、MIME、文件名和大小；语音兼容 `voice_wav_url` 与 `asr_refer_text`。不能按扩展名断定语音格式；腾讯 ASR 文本只作为外部内容，不作为指令。下载遵守现有 SSRF 防护、重定向逐跳检查、限时/限量/格式检测，不向附件 URL 携带 AppSecret 或平台 token。
- QQ 表情：正文里写作 `<faceType=1,faceId="306",ext="<base64>">`，`ext` 解码后是 `{"text":"牛气冲天"}`，名字可以直接拿到。`faceType=3` 是超级表情；`faceType=6` 是收藏的表情包，名字为空，图片在 `attachments`。
- 合并转发：`message_type = 102`，有递归 `msg_elements` 就保留作者、顺序和来源，否则用文字记录；设置深度与体积上限。
- 卡片：`message_type = 3`，保留 `ark_data` 的标题、来源、摘要和合法跳转链接。`jump_url` 是否存在取决于卡片类型和实测，缺失时不编造，也不拿预览图片地址假装原帖地址。
- 机器人自己的消息：max 实测网关**不会**推回机器人自己发的消息；另一个项目（Diana PR #883）说开了全量消息后**会**推回。【待实测】，所以【必须】按「可能推回」来防御：入站消息如果和账本里已记录的出站消息 id 相同，或作者是机器人自己，就当作回声对账，**不触发回合**。

### 3.5 发送规则（文档已核对，本账号尚未实测）

| 场景 | 被动回复窗口 | 主动发送 |
|---|---|---|
| 群聊 | 收到消息后 **5 分钟**内，同一条消息最多回复 **5 次**；第 6 次报 40034128（max 实测） | 群主打开「机器人主动发言」后可以发。没打开时，社区普遍报 40034105「主动消息失败，无权限」 |
| 私聊 | **60 分钟**内，每条最多 **4 次** | 允许，但用户可以在客户端关闭「允许主动发送」（会收到 `C2C_MSG_REJECT`） |

- 被动回复：请求带 `msg_id`（或针对事件的 `event_id`），同一个 `msg_id` 下每次回复的 `msg_seq` **必须不同**，取值范围 0–65535。腾讯 SDK 用随机数；本方案按次递增（从 1 开始），确定、可测，也不会超出范围。
- 官方概述的主动发送频控：认证群聊 Bot 维度 60/qpm，未认证 30/qpm，单群 20/qpm、1000 条/日；认证私聊 Bot 维度 10/qps，未认证 5/qps 且 30/qpm，单用户 20/qpm、1000 条/日。分认证类型、场景和发送方式配置，不把主动额度说成所有消息统一限制；被动发送仍遵守窗口和实际服务端频控。
- 分钟速率可用有界令牌桶；每日上限用持久化计数与重置时间，不能用不断回填的桶冒充每日配额。平台日界不明确时保守等待并标记待实测，不能重启清零。用户请求的结果在截止时间内排队，过时主动插话/OTP 不进入通用等待队列。
- 官方文档说明相同 `msg_id + msg_seq` 重复发送会失败，但这不等于能查回原消息回执。只有确认去重作用域、期限、相同 payload 约束与可对账依据后，才允许受控使用同一发送尝试恢复；否则仍标 `ambiguous`，不自动换 msg_seq 重发。

### 3.6 出站消息格式

- 发送接口：`POST /v2/groups/{group_openid}/messages`、`POST /v2/users/{user_openid}/messages`。
- `msg_type`：0 纯文本，2 Markdown，7 富媒体。返回 `id`（用于撤回、记账）和 `ext_info.ref_idx`（用于以后被引用）。
- **Markdown**：使用 `msg_type = 2`、`markdown.content`；代码块、表格、公式和长度按 M0 分项验证。公式转换必须避开代码块和转义文本。仅明确的格式拒绝且可确定未接受时退回纯文本一次；超时、结果不明或内容违规都不能通过纯文本再发一遍。
- **@**：`<qqbot-at-user id="{member_openid}" />`，在 Markdown 里显示为 @群名片（max 实测）。
- **引用**：`message_reference.message_id` 填被引用消息的 `msg_idx`（入站消息）或 `ref_idx`（机器人自己的消息）。
- **QQ 表情发不出去**：`<faceType=…>` 和 `<emoji:id>` 都会原样显示。表情一律换成 `[名字]` 文字，没有名字就写 `[表情306]`。
- **撤回**：`DELETE /v2/groups/{group_openid}/messages/{message_id}` 和 `/v2/users/{user_openid}/messages/{message_id}`。第三方记录为自身消息两分钟、管理员可撤他人；不能据此推断本账号所有群可用，M7 分别验证时限、目标和授权。
- **内容审核**：明确的内容违规拒绝不重试。机器人是否可申诉以后台为准，不承诺解封，也不设计绕过审核。
- 以后可能用到、v1 不做的接口（腾讯 SDK `routes.ts`）：私聊流式消息 `POST /v2/users/{openid}/stream_messages`；按钮回调确认 `PUT /interactions/{interaction_id}`。

### 3.7 富媒体和文件（文档、SDK 与目标场景一起验证）

- 先上传，拿 `file_info`，再发 `msg_type = 7`、`media: {"file_info": ...}` 的消息；`content` 是图片下面的说明文字。
- 上传接口：`POST /v2/groups/{group_openid}/files`、`POST /v2/users/{user_openid}/files`，body `{"file_type": 1|2|3|4, "url" 或 "file_data"(base64), "srv_send_msg": false}`。`file_type`：1 图片，2 视频，3 语音，4 文件。**`file_type = 4` 时必须带 `file_name`**（腾讯 SDK 会自动补上）。**群聊和私聊的上传接口不能混用**。
- `file_info` 按「AppID + 环境 + 目标类型/端点 + 内容哈希 + 文件类型」缓存，按返回 ttl 提前失效。上传成功不是消息发送成功。
- 小文件和分片上传均需支持；分片 prepare、PUT、part_finish、合并过程有独立恢复记录。阈值、单片大小、类型上限来自固定版本契约和探测，不把单请求上限等同附件总上限。对 40093001/40093002 记录发生阶段；SDK 的 fallback 常量不能单独证明“当天额度耗尽”，未知原因不永久关闭整个 AppID 的文件能力。
- 腾讯 OpenClaw 插件的 README 写：图片最大 20MB，视频最大 30MB，附件最大 100MB，每日累计最多 2GB。
- 语音发送列为可选能力：核实目标接受的 MIME/编码、大小和是否需要转换；不凭“大概率 SILK”写死。
- **发文件（`file_type = 4`）【上传已有限实测；发送/下载待验】**：2026-10-29，授权测试群的固定小 PDF、ZIP 均上传成功（`srv_send_msg=false`），未发送文件消息。此证据不证明其他群、尺寸或实际下载可用。SDK/插件有实现，第三方 50015014 记录也不能直接套用为本账号结论。支持原生文件与下载链接两条路线，实际部署至少一条完整验收可用（见 M6 和实施进度）。

### 3.8 服务范围、身份与有效能力

- 未认证、个人认证、企业认证按后台分别处理；本账号个人认证后可配置公开服务，进群上限页面显示 500。公开私聊与公开进群目前关闭，不由接入程序代开。
- 私聊是否允许取决于腾讯服务范围、体验名单及本地访问策略；不再硬编码“所有个人机器人只能和体验用户聊”。腾讯允许添加好友不自动授予本地模型费用、文件或服务器操作权限。
- 身份没有 QQ 号，只有按机器人分配的 openid：群是 `group_openid`；群成员是 `member_openid`，**同一个人在不同群里不一样**；私聊是 `user_openid`，又是另一个。能跨群的 `union_openid` 要单独申请，本方案不用。
- v1 默认不依赖尚未验证的贴表情、戳一戳、编辑消息、群名片、原生 QQ 表情、群文件区、完整历史或撤回通知；“当前未支持”不等于平台永远没有。成员名册接口需单独验证，不能从禁言权限推导。
- 群管理按 0.3 节的后台入口接入：撤回、入群申请查询/审批、禁言、移除成员。实际权限是「平台支持 ∩ 开发者允许申请 ∩ 目标群授权 ∩ 本地操作者权限 ∩ 工具策略」，缺一不可。
- 有效能力状态至少包含 `unknown / available / denied / temporarily_unavailable`、证据来源、目标、观测时间及复核期限。群管理和敏感访问未知时拒绝；普通 Markdown 等无副作用探测按明确降级规则处理。
- owner 是本系统最高管理员，不把群主角色自动变成本系统 owner；首次绑定后无需每次证明身份。建隔离沙盒、写代码、发送用户要求的产物按既有授权策略自动执行，不因迁移新增逐步 OTP。重要宿主机操作沿用有范围、有效期和撤销能力的任务授权。

---

## 4. 目标架构

```
腾讯网关 / REST
      │
src/qq_official/             纯协议库：token、网关、事件解析、发送、上传
      │                       不 import gaojibot，不 import nonebot
      ▼
adapters/qq_official/         平台适配：官方事件 ⇄ gaojibot 自己的格式
      │                       （MessageBody、ConversationScope、账本身份、投递）
      ▼
平台中立的回合入口（M1）  ←──  adapters/onebot_v11.py（现有 OneBot 路径，行为不变）
      │
gaojibot 核心：账本、上下文、ChatOrchestrator、工具、Sub-Agent、投递队列……
```

### 4.1 目录

```
src/qq_official/                       # 【必须】不依赖 gaojibot 和 nonebot
  __init__.py
  credentials.py      # 从文件读取 AppID/AppSecret，检查权限，repr 不输出密钥
  token.py            # TokenManager
  http.py             # RestClient：鉴权头、body 错误码解析、超时、错误分类
  errors.py           # 错误码表和分类（第 6 节 M3 的表）
  gateway.py          # GatewayConnection：状态机、心跳、Resume、退避
  events.py           # Dispatch → 带类型的 dataclass
  outgoing.py         # 出站 payload 构造：text/markdown/media/reference/@
  media.py            # 小文件上传、分片上传、file_info 缓存接口
  probe.py            # M0 的实测探测工具（命令行）
  testing/
    fake_platform.py  # 测试用的假网关和假 REST 服务（aiohttp）

src/plugins/ai_chat/adapters/qq_official/
  __init__.py
  codec.py            # 官方事件 ⇄ MessageBody
  ingest.py           # 入账、去重、回声识别、绑定检查、触发判断
  inbox.py            # 持久入站、恢复序号、回合派发；复用现有存储约定
  sender.py           # 回复窗口、额度、发送顺序、错误映射、Markdown 降级
  windows.py          # 回复窗口存储
  rate_limit.py       # 额度令牌桶
  binding.py          # /绑定 流程
  authority.py        # owner 识别
  capabilities.py     # 账号/群有效授权与证据，不是一个全局 bool
  migration.py        # 历史会话迁移、路由版本与回退日志
  files.py            # 文件交付：原生 / 链接 / 降级
  commands.py         # 命令分发接线
  runtime.py          # 启动、停止、健康状态、后台任务
```

【自行判断】文件可以拆得更细或合并，但 `src/qq_official/` 不依赖 gaojibot 这条边界不能破。【必须】加一个测试，扫描 `src/qq_official/` 下所有文件的 import，出现 `nonebot` 或 `src.plugins` 就失败。

### 4.2 关键类型（示意，名字可调整）

先明确三种不同的标识，不能只给旧 `ConversationScope` 换一个 platform：

- **账号**：`AccountKey = (platform, app_id, environment)`；可以用本地稳定账号 ID 引用这组三元组。
- **收发端点**：`EndpointKey = (account_key, kind, native_conversation_id)`；相同 openid 字面值在不同账号、环境中也不能混用。
- **操作者**：`ActorKey = (account_key, identity_kind, identity_namespace, native_user_id)`；群成员的 namespace 包含所属群，私聊身份独立存储。平台群主角色和本地 owner 不是同一字段。

【必须】端点是「从哪里收、向哪里发」，canonical conversation 是「查哪一份历史」。历史绑定不能改变实际发送账号、回复凭证或授权主体。现有 OneBot scope key 保留；官方身份用结构化字段与规范序列化，不拼接歧义字符串。

```python
# src/plugins/ai_chat/adapters/platform_types.py（M1 引入）
# 下列 AccountKey / EndpointKey / ActorKey 按上面的结构定义。

@dataclass(frozen=True)
class ReplyCredential:
    endpoint: EndpointKey
    kind: Literal["msg_id", "event_id"]
    value: str
    issued_at: int | None
    received_at: int
    expires_at: int           # 已减安全余量，Resume 重放不得重置有效期
    max_replies: int

@dataclass(frozen=True)
class InboundMessage:
    endpoint: EndpointKey
    actor: ActorKey
    event_id: str | None
    native_message_id: str
    sender_display: str
    platform_role: Literal["owner", "admin", "member", ""]
    body: MessageBody
    mentions_bot: bool
    reply_to_native_message_id: str | None
    occurred_at: int
    reply_credential: ReplyCredential | None
    # gateway session/seq、原始事件和引用索引放在适配器入站记录中，
    # 不把 raw 或平台凭证直接塞进模型上下文。

@dataclass(frozen=True)
class PlatformFeatures:
    """实现层能支持什么；不代表某个群或某个人已获授权。"""
    group_files: bool
    reactions: bool
    forward_messages: bool
    member_list: bool
    native_ocr: bool
    inline_voice_text: bool
    markdown: bool
    native_files: bool
    proactive_group: bool

@dataclass(frozen=True)
class EffectiveCapabilities:
    endpoint: EndpointKey
    revision: int
    grants: Mapping[str, "CapabilityEvidence"]
    # 每项含状态、来源、观测时间、复核期限，按 actor 再做本地鉴权。

class TurnPlatform(Protocol):
    """绑定到具体账号，OneBot 和 qq-official 各实现一份。"""
    account: AccountKey
    capabilities: OutboundCapabilities
    features: PlatformFeatures
    async def ready(self, endpoint: EndpointKey) -> str | None: ...
    async def effective_capabilities(self, endpoint: EndpointKey) -> EffectiveCapabilities: ...
    async def send(self, request: SendRequest) -> SendOutcome: ...
    async def recall(self, endpoint: EndpointKey, native_message_id: str) -> bool: ...
```

消息引用、媒体句柄、任务发起人、项目权限和回执都携带账号与端点。`SendRequest` 还需包含投递 ID、路由版本和操作者；执行时复查，不能把模型传来的任意 openid 当可信目标。

---

## 5. 不变量（防 bug 的底线）

下面每一条都要有对应的测试（第 10 节）。

1. **旧路径隔离**：关闭官方功能且未迁移的 OneBot 会话，行为不变；快照固定时间、随机数等变量后比较，不以放宽断言掩盖回归。
2. **入站可靠接管**：收到并持久化的事件只创建一个逻辑回合。去重、入账和可恢复派发一起设计；不是先记“已处理”、再用内存 task 启动业务。
3. **发送结果不明不重放**：请求可能已被平台接受时标 `ambiguous`，不能自动换序号补发。只重试有明确“未接受”证据的尝试，不能把所有 4xx 或重复错误都视为未发送。
4. **窗口原子分配**：窗口、序号、分片发送意图和 payload 哈希一起持久化。并发不超次数，恢复不把第一个分片从头再发；重放旧入站不延长窗口。
5. **配额不因重启清零**：按账号、场景、端点、时间周期控制额度；平台限流优先。排队不长时间持有数据库锁或投递租约。
6. **等待有类别和期限**：持久任务结果可等窗口，默认最多 24 小时；过时插话丢弃，OTP 过期失效。平台无法补回的入站缺口、投递过期和结果不明都明确展示，不能承诺绝对不丢。
7. **身份与数据隔离**：openid 不转整数；不同 AppID、环境、群、身份类型严格分开。历史会话合并不自动合并个人记忆与权限。
8. **未绑定群不消费业务**：仅处理有限的绑定命令、生命周期和必要安全元数据；不存普通聊天正文、不跑 LLM、不自动回复。公开入群也不等于自动启用本地业务。
9. **首次身份可信**：owner 由可信控制台/配置首次指定，不采用“第一个私聊的人就是管理员”。群成员映射需双向证明；绑定码不是永久管理令牌。
10. **凭证不外泄**：平台密钥、access token、绑定码、OTP、临时下载凭证不进模型、公开日志、异常 repr、后台列表或测试快照。
11. **能力与授权分开**：不支持/未验证的工具不向模型承诺；即便 Tool Call 绕过可见列表，宿主仍检查目标与权限。群主不自动拥有服务器管理权。
12. **@ 和引用不可伪造**：来自平台的结构化证据才算原生 @、作者或引用关系；普通文字里的平台标签转义，引用查找限制在同一端点。
13. **单实例有边界**：共享 PostgreSQL 内同账号、同环境互斥，失锁立即停止网关和出站；两套独立数据库不能靠 advisory lock 互斥，必须使用不同 AppID 或明确停旧接新。
14. **项目访问须证明**：说过话的人不等于当前群成员。项目、文件与高风险工具使用独立可撤销授权，任务恢复和文件发送时重新检查。
15. **任务完成不等于交付完成**：生成、保存、上传、消息被接受、用户下载分别记账。只有真实回执才能推进对应阶段，最终文字从当前交付状态生成。
16. **迁移与回退可审计**：同一逻辑会话只有一个活动出站路由。切换有版本和停旧检查；不重发已成功消息，不把结果不明的发送搬到新群重试。
17. **文件清理与有效期一致**：链接有效期内文件必须存在；已确认交付后的副本默认最多留一小时。未交付文件也有容量与截止期限，不能无限堆积或因沙盒销毁而丢失。

---

## 6. 里程碑

保留 M0–M9 编号方便追踪，**执行顺序改为 M0 → M1 → M2 → M3 → M4 → M6 → M5 → M7 → M8 → M9**。

| 阶段 | 先得到什么 | 不满足时不做什么 |
|---|---|---|
| M0 | 最小协议契约、离线探测工具 | 没有真实报告，不把待实测能力写成已开放 |
| M1–M4 | 新测试会话里的身份、入站、文字和持久投递 | 不导入旧历史，不开放宿主机操作 |
| M6 | 至少一种可实际领取的文件交付方式 | 没有可用出口，不承诺“完成后发文件” |
| M5 | 项目权限与可回退的历史迁移 | 不用历史映射代替权限，不直接改 outbox 目标 |
| M7–M9 | 可选能力、产品配置、回归与验收 | 本地通过不等于生产上线 |

编码与假服务测试可以先行；每个涉及真实权限的上线门槛必须由用户受控验收补齐。**第一次真机试用用全新的测试会话，不先搬生产历史。**

### M0：协议库、假服务器、实测探测工具

**目标**：先验证鉴权、网关、消息结构和文字收发契约，提供用户自助探测工具；媒体、分片和群管理按后续里程碑扩展，不把 M0 做成全部功能的大重写。

**内容**：

1. `src/qq_official/` 的凭证、token、HTTP、网关、消息解析及最小发送模块（见 4.1）。
   - HTTP 用仓库已有的 `httpx`，WebSocket 用已有的 `websockets`。【自行判断】也可以用已有的 `aiohttp`，但不要引入新依赖；确实需要时在进度记录里说明。
   - 时钟、随机数、sleep 全部可注入，测试里不真的等待。
   - `TokenManager`：并发调用只换一次 token（加锁，single-flight）；换 token 失败要退避重试；提供 `invalidate()` 给遇到 401 时使用。
   - `GatewayConnection` 的状态机：`connecting → identifying → ready → resuming → closed`；心跳 ACK 超时就重连；op 7 重连；op 9 重新 Identify；按 close code 区分「可以 Resume」和「必须重新 Identify」；重连退避有上限，并带随机抖动；对外暴露事件流和连接状态（给健康检查用）。
   - 事件去重**不放在协议库里**（交给适配器做，因为要持久化），但协议库要把 `s`（序号）和事件 `id` 原样交出去。
2. `src/qq_official/testing/fake_platform.py`：假的 token 接口、网关和 REST 服务。要能模拟：
   - Hello/Identify/READY/心跳/ACK，op 7，op 9，Resume 成功和失败，ACK 不回；
   - 群聊和私聊消息事件、拉群和退群事件、主动发言开关事件；
   - 发送接口按第 3.5 节的规则校验窗口、`msg_seq`、额度，并返回相应错误码；
   - 上传、分片及撤回接口的夹具随 M6/M7 补齐；
   - 在 body 里返回错误的 HTTP 200 响应；
   - 「请求已处理但响应丢失」（用于测不重发）。
3. `python -m src.qq_official.probe`：给用户运行的探测工具。
   - 参数：`--app-id-file`、`--app-secret-file`、`--group <group_openid>`（可选）、`--user <user_openid>`（可选）、`--sandbox`、`--report <路径>`。
   - 不带目标时只连接并监听，不发消息；提供受保护的本机身份报告，默认控制台日志不打印原始 openid。
   - 显式选定测试端点后展示本批探测项、预计消息数和影响，确认一次后执行；换目标或增加有副作用项目重新确认，不逐条弹确认。
   - 探测项随 M0/M6/M7 分批提供，未实现项标“待补”，不是 M0 一次做完；逐项输出通过/失败/错误码并写入报告：
     1. 收到的群消息事件类型（是否开了全量消息）；
     2. 正常被动回复及回执字段；窗口超限主要用假服务测试，真实上限探测另行勾选；
     3. 同 `msg_seq` 的去重验证单独选择，不默认故意触发平台限制；
     4. 用**不 @ 机器人**的消息的 `msg_id` 做被动回复；
     5. 不带 `msg_id` 主动发送（验证群主动发言开关和错误码）；
     6. Markdown 表格、代码块、公式分别记录；长度探测可选、有限次数，不把每种格式都假定已支持；
     7. 上传并发送图片；
     8. 文件：合法 `file_name` 的 PDF 和 zip，群聊/私聊分别验证上传与发送；缺字段拒绝在假服务测试，不默认发送无效请求；
     9. 撤回刚发的消息；
     10. 记录机器人自己的消息是否被推回；
     11. 语音附件的 wav 地址和识别文本的字段名。
   - 不同功能使用新消息的有效窗口，不能前面耗光五次回复后，把后面的文件/Markdown 误判成不支持。每项记录账号环境、目标类别、阶段、回执、错误码和时间。
   - 报告以稳定假名替换身份；清除凭证、签名 URL、绑定码和聊天正文隐私。显式 `--raw` 仅写本机受保护文件、不打印、不提交。真实禁言、踢人不属于默认探测。

**测试**：协议库全部单元测试只连假服务器；覆盖上面列出的每一种模拟情况。

**完成标准**：
- 协议库能连上假服务器，收发和重连测试全部通过；
- import 边界测试通过；
- 探测工具在假服务器上端到端跑通；
- 写好 `docs/qq-official-probe.md`，告诉用户怎么准备测试群、体验用户名单，以及怎么运行探测工具。

> 用户跑完探测后回填第 3 节，按目标范围开启能力。没有报告时可以继续开发离线分支，但不能把“全量群消息”“主动发送”“原生附件”等记成该账号验收通过。

### M1：回合入口解耦（OneBot 行为不变）

**目标**：抽出最小平台端口，让文字回合能经官方适配器运行。按依赖逐步迁移，不要求先改完所有 OneBot 文件才开始联调；不借机重构无关模块。

**现有耦合，实施前重新核对**：
- `application/chat_orchestrator.py` 已经有 `ChatPorts`，但 `run(bot, event, ...)` 里仍然直接读 `int(event.user_id)`、`int(event.message_id)`；
- `tools/state.py` 的 `ToolContext.event: MessageEvent`、`TurnState.bot: Bot | None`；
- `tasks.py` 的 `register_current(user_id: int, group_id: int | None, message_id: int)`；
- `config.py` 的 `enabled_groups: set[int]`、`admin_user_ids: set[int]`；
- `ConversationScope`/`OutboundCapabilities` 可以复用，但 scope key 没有账号维度，账本 identity 查询也不能直接接收裸 openid；按 4.2 节补隔离，不宣称现成类型已经足够。
- `project_runtime.py` 仍校验 OneBot 和数字 ID，依赖 `get_group_member_list`；`project_file_transport.py` 固定原 bot 及群文件回执。项目/文件不是改一个 target 就可用，分别由 M5/M6 收口。

**步骤**：

1. **先建回归安全网，再动代码**【必须】：
   - 为 OneBot 路径补代表性出站快照：普通回复、引用、@、图片、长回复、Sub-Agent、提醒、文件。固定时钟和随机 ID 后，对比最终 `send_group_msg`/`send_private_msg`/`call_api` 参数；不直接比较真实时间戳。
   - 跑一遍全套测试，记下通过数作为基线。
2. 引入 `InboundMessage`、`ReplyCredential`、`PlatformFeatures`、`TurnPlatform`（4.2 节），先只实现 OneBot 版本（用现有代码包装，不改逻辑）。
3. 把回合主路径改成接收 `InboundMessage` + `TurnPlatform`：
   - `ChatOrchestrator.run`、`ChatPorts`；
   - `tasks.register_current` 接收带账号/端点的身份；OneBot 保留兼容调用口与既有存储语义，不能只把类型注解改成 `str`；
   - `ToolContext` 增加 `inbound: InboundMessage` 和 `platform: TurnPlatform`。过渡期 `event` 保留为 `Optional`，只有 OneBot 专用的工具才读它；
   - `tool_executor._ask_ai` 及其调用链。
4. 群开关、群级设置从「int 群号」改成「scope key」：OneBot 的 scope key 由群号生成，旧数据和旧配置都不受影响。
5. 工具按平台能力筛选：
   - 在 `tools/registry.py` 的 `ToolHandler.enabled(ctx)` 里检查 `ctx.platform.features`；
   - 需要群文件、贴表情、合并转发、群成员列表、NapCat OCR、NapCat 语音转文字的工具，各自声明依赖哪项能力；
   - 提示词里关于平台能力的描述（表情、贴表情、`[silence:…]` 之类）也从 `features` 和 `capabilities` 生成。
6. 命令：先抽绑定、帮助、模型、effort、用量、任务状态/取消的公共分发；项目/权限命令到 M5 再接，其他命令到 M7。OneBot matcher 包装原行为，不能构造假的 OneBot Event。

7. **清理「群号、QQ 号一定是数字」的假设**【必须】。官方的 openid 是字符串，这些地方遇到会直接报 `ValueError` 或者匹配不上。2026-10-28 已经发现的有：
   - `application/chat_orchestrator.py`：`int(event.user_id)`、`int(event.message_id)`；
   - `tasks.py`：`register_current(user_id: int, group_id: int | None, message_id: int)`；
   - `handler_constants.py`：`GROUP_CONVERSATION_ID_PATTERN = r"^group:(\d+):user:(\d+)$"`；
   - `admin_api/model_views.py`：`^group:(\d+):user:(\d+)$`、`^group:(\d+):(?:default|enabled|vision-auto-describe)$`、`^group:(\d+):member-default$`（qq-official 的群级设置在后台会显示不出来）；
   - `trigger_service.py` 约 318–330 行解析 scope key 的分支；
   - `onebot_chat.py` 的 `_conversation_id` 使用数字群号/用户号。官方使用含账号、环境、身份类型的规范 key；旧 OneBot key 不重写，不沿用缺少账号的简单拼接格式；
   - `config.py` 的 `enabled_groups: set[int]`、`admin_user_ids: set[int]` 等 `_get_group_ids` 读出来的整数集合。

   【必须】搜索上述假设并列出实际官方可达路径；用非数字 ID 和“不同账号、相同 openid 字面值”测试隔离。仅 OneBot 可达的数字逻辑可保留，不为清零搜索结果做全仓机械替换。

【自行判断】具体怎么切、分几个提交。建议按「主回合路径 → 工具上下文 → 命令 → 其余」的顺序，每步都保持全绿。

【必须】不许出现以下做法：
- 把 openid 转成整数，或者编造假 QQ 号塞进 OneBot 的数据结构；
- 为了让 qq-official 跑起来，去构造假的 `GroupMessageEvent`；
- 在 M1 里改动 OneBot 的出站内容。

**完成标准**：回归通过；已提取的公共回合端口和协议库不 import OneBot（边界测试）；官方最小回合不读取 legacy event。过渡的 OneBot 专用实现可保留，剩余耦合逐项分配到 M5/M6/M7，不能让官方路径悄悄调用。

### M2：入站、首次身份与新会话绑定

**目标**：先把新测试会话接入，不依赖 M5 才能绑定，也不在此时迁移旧群。业务回复到 M3/M4 接通，绑定流程先用假发送端口测试。

1. **首次 owner 引导**：由已认证的本地管理控制台或受保护配置指定 `(account, C2C user_openid)`。允许探测工具或有限的 `/whoami` 返回本人身份，但这不授予权限。不能通过“第一次私聊”自动认领 owner；现有 OneBot owner 也不能仅凭同名昵称自动匹配。
2. **新群绑定**：可信 owner 私聊申请一次性 challenge；在群里提交码后，仅建立包含目标账号、群、实际发令者的待确认候选。owner 在原私聊/可信控制台确认候选，事务内激活新会话。是否把该群发令者关联为 owner 必须明确展示并确认；普通群绑定不自动授予发令者 owner。
   - 建议 challenge ID + 六位随机码，十分钟有效；用服务端 pepper 的 HMAC 存储，常量时间校验，单次消费。
   - 尝试限制按账号、发令者、端点及 challenge 组合实施，包含不存在的 challenge；不能只对“猜中的那条记录”计错。设置总速率和创建频率上限。
   - 群里公开的码被抢先使用时，原 owner 可拒绝候选并重新申请；未经第二端确认不得读取历史或获取管理权限。
   - challenge 和候选持久化，重启不改变确认对象；绑定后长期识别，普通聊天不反复确认。
3. **事件转换**：按 3.4 节处理文本、真实 @、表情、媒体、引用、递归转发和卡片。引用只能在同账号同端点内查精确索引；查不到保留证据、不编造作者。未知元素限量降级为文字/占位；非法或过大 payload 隔离并记录原因，不能无限重试堵住队列。
4. **持久入站**：
   - 网关接收后，事务写 inbox 和对应恢复点；Resume 只使用已可靠接收的连续 seq。未绑定普通聊天只记录必要的跳过标记，不存正文。
   - 消息 ID 与事件 ID 分别去重；没有某类 ID 时不使用公共空字符串建立去重键。生命周期也可幂等处理。
   - worker 领取 inbox，将账本、引用索引、窗口和唯一回合派发意图一起提交；随后由已有任务机制恢复执行，不用裸 `create_task` 作为唯一凭据。
   - `(account, endpoint, outbound_message_id)` 命中或可信机器人作者命中时做回声对账；发送中回声先存证再匹配，不能触发新回合。
   - 全量群消息进入当前群的连续时间线；被 @、引用或允许私聊触发时读取它。不因某消息没有 @ 就排除上下文，也不因为全量接收就自动开主动发言。
5. **生命周期与撤权**：被移出群时失效该端点的窗口、授权和待发送路由，保留审计；重新入群不无条件恢复旧敏感授权。主动消息拒绝只禁止主动方式，不等于退群。断线期间状态可能过时，恢复后复核或标未知。
6. **运行时**：复用 `BackgroundTaskSupervisor`，与 OneBot 连接独立启停。PostgreSQL 独占连接持有按账号/环境派生的会话锁，不放回普通连接池；拿不到锁不连网关。取得锁后更新 fencing epoch，收发前检查当前 epoch；锁连接丢失时关闭网关并停止派发/发送。已在途 HTTP 仍按结果不明处理。

【必须】该锁只保护**同一数据库**。App 与服务器各用独立数据库时，使用不同 AppID；迁移同 AppID 必须停旧实例并核对，再启新实例。不添加“全世界只有一个进程”的虚假保证。

**验收**：假服务全消息类型；任意持久化边界重启后可接管；连续 seq 不跨越未落库事件；重复/回声只创建一个回合；首次 owner 不能抢注；绑定码抢用不提权；失锁停止；未绑定群不存正文、不调用模型。

### M3：qq-official 出站（发送器）

**目标**：直接回复、后台任务、提醒和文件统一经过 `sender.py` 与现有持久 outbox；优先扩展现有投递记录，不另造一套互不对账的队列。

**内容**：

1. **先冻结发送计划**：把一个逻辑结果转成有限的分片/附件清单，存稳定 ID、序号、payload 哈希、目标账号/端点、路由版本。重启读取同一计划，不重新调用模型生成另一份然后从头发送。每个分片有独立状态与回执。
2. **选择发送方式**：原消息的有效窗口 → 同端点最新合法窗口（不伪造引用）→ 已获允许的主动发送 → 等窗口。新窗口是否适用于该种回复以探测结果为准。主动状态未知时，只能针对待交付的正常用户请求做一次受控尝试，不在启动时发测试消息、不重复试探已拒绝的群。
3. **原子意图与窗口**：
   - 先等待账号/端点频控，再短事务锁窗口；检查 epoch、授权、窗口到期和次数，同时登记发送尝试、分配 `msg_seq`、更新计数。
   - 回复时限依据原始事件时间及保守时钟偏差计算，预留安全余量；Resume 重放不能按重放时间重新起算。
   - 网络请求在事务外，出发前再次检查期限和租约。已分配序号不随意复用；经验证可恢复的同一尝试必须保持 payload 不变。
   - 若重启时无法区分“意图已写但尚未发”和“已发未记回执”，保守标结果不明；无法在腾讯无对账能力时承诺 exactly-once。可证明未出发才恢复普通排队。
4. **分类排队与预算**：
   - 当前回合最终答案与用户要求的附件优先，预留回复次数；旧任务结果使用余量。端点调度可按优先级，但同一结果的分片顺序固定，不能被一条等待旧消息永远挡住。
   - 关闭逐 token/逐段流式消息；`say` 进度合并、节流，默认不消耗宝贵回复次数。长结果优先“简短结论 + 完整文件/链接”，不能切成几十条卡死五次窗口。
   - 持久结果最多等配置期限；主动插话过时丢弃；OTP 无法及时送达则取消该挑战。`defer_unsent` **仅用于确认未发送**，不得用于已发请求或结果不明。
   - 配额按 3.5 节分场景；日计数持久化，不能重启清零。等待用 `next_attempt_at`/唤醒信号，不持锁 sleep，也不长期占住 `claim_due` 租约。
5. **渲染**：遵守 3.6 节。分片保留代码围栏和 Unicode 边界；公式转换跳过代码。只有经端点校验的 mention 节点生成原生 @，普通文本里的 `<qqbot-…>` 等转义。明确格式拒绝且未接受才降级纯文本一次，审核拒绝不可降级绕过。
6. **错误映射**：同时记录 HTTP 状态、腾讯错误码、阶段、request ID、是否确认未接受；敏感内容脱敏。未知错误不得仅凭数字猜原因。

| 情况 | 处理 |
|---|---|
| 能证明请求未出发，如 DNS/建连失败 | 未发送，退避；token 获取失败不算一次消息发送 |
| 发送后超时、断线、无明确拒绝证据的 5xx | `ambiguous`；有可靠对账才推进，否则展示待核实，不自动补发 |
| 明确内容违规，如 40034006 | 本内容终止，记录原因；不换格式、账号或渠道重试 |
| 已验证的回复超限，如 40034128 | 关闭该窗口；确认未接受后重新选择合法发送方式 |
| 已验证的主动权限拒绝，如 40034105 | 更新该目标权限状态，任务结果等窗口，不影响其他群 |
| 明确未接受的限流/额度错误 | 按服务端期限或有界退避等待，更新对应维度计数 |
| 明确鉴权拒绝 | single-flight 刷新 token 后重试一次；持续失败暂停并诊断 |
| 明确 IP 白名单拒绝 | 给出错误及需要核对的出口 IP，不擅自改网络 |
| 原生文件上传明确不支持，如实测 50015014 | 仅标对应账号/端点/类型暂不可用，按 M6 选链接；允许复核恢复 |
| 40093001/40093002 等上传阶段码 | 按固定版本契约分类；仅 SDK fallback 名称不能认定日额度。上传未发消息时可改交付路线 |
| 重复序号或其他未知错误 | 不当成成功回执；隔离、保留证据、人工核实，不盲重试 |

7. **回执提交**：收到成功响应后，发送尝试、账本、消息索引、`mark_committed`、`link_send` 在同一事务或现有可靠事件机制下幂等完成。响应丢失/落库前宕机不能假装从未发送。适配现有 `begin_direct_attempt`，不要绕过它另发一次。
8. **撤回**：区分撤回自身消息和群管理撤回，使用实际消息时间、目标权限与已核实限额；撤回不是普通发送失败的自动补偿。

**验收**：并发窗口、跨账号隔离、seq 唯一、断点恢复、分片中途宕机、服务已接受但响应丢失、回执落库失败、旧结果不饿死当前回复、过期 OTP 不补发，以及每一条错误分类。

### M4：回合跑通

**目标**：在 qq-official 的群聊和私聊里，从收到消息到回复，完整跑通一个回合，工具按平台能力筛选。

**内容**：
1. 实现账号绑定的 `TurnPlatform`；`ready(endpoint)` 区分连接、凭证、目标绑定/权限和待窗口，不只检查“网关在线”。
2. 用 M1 改造后的 `ChatOrchestrator` 跑回合，回复交给 M3 的发送器。
3. 首批命令：`/绑定`（`/bind`）、`/帮助`、`/model`、`/effort`、`/usage`、任务状态和取消本人任务。每条命令保留原有配置权限；不能因为支持 `/model` 就允许任何人改全群设置。
   - 【自行判断】其余命令要不要迁移。不迁移的命令统一回复「这个命令在官方机器人上暂不支持」，不要静默失败。
   - `/项目`、跨身份记忆与 `/shell` 等在 M5 完成权限适配后开放；`/例行`、`/群百科` 等在 M7 对齐。未完成能力显式标未支持。
4. 工具筛选：确认 qq-official 回合里看不到群文件、贴表情、合并转发、群成员列表、NapCat OCR、NapCat 语音转文字相关的工具。
5. 后台结果按真实账号/端点交给 M3；`exclude_platforms`、就绪状态和 worker 启动不依赖 OneBot 登录。没有 OneBot Bot 实例时也能启动、重启接管及发送官方任务。
6. 此时只用新测试会话。文件任务先以假产物验证队列，真实文件承诺须过 M6；迁旧历史和生产权限须过 M5。

**测试**：端到端集成测试：假服务器推一条 @ 消息 → 入账 → 假 LLM 生成回答 → 假服务器收到正确的被动回复（`msg_id`、`msg_seq`、Markdown）。再测一个超过 5 分钟才完成的 Sub-Agent：群主动发言开着时主动发出；关着时挂起，下一条群消息到达时发出。

### M5：项目权限、历史迁移与任务续接（在 M6 后执行）

**目标**：复用 M2 的身份引导，接通项目和宿主机操作；旧历史可读、旧任务可续，但不能串身份、重复发送或把私密资料搬到错误的群。

1. **统一权限入口**：扩展现有 `Authority`，接收完整 ActorKey、端点和操作类型。OneBot 沿用原规则；官方 owner 依据可信 C2C 配置和已确认群身份关联。适配 `qq_action_authorization`、`server_task_authorization`、`fleet_authorization`、`mobile_authorization` 等调用点。不能把裸 openid、昵称、群主角色当作系统管理员。
2. **项目是独立子工程，不能声称和平台无关**：
   - `project_access.py` 的 Event 类型判断、`project_runtime.resolve_access/_execution_access` 的平台/数字 ID 检查与 `get_bots()` 依赖，要改为平台访问端口；保留 OneBot 原实现。
   - 完整成员名册若没有已验证接口，就不能用“曾经发过言的人”冒充。可信新入站只证明该时刻的发言者；项目成员通过 owner 的显式 grant 管理，记录账号、actor、项目、能力、有效期、证据和撤销版本。
   - 后台任务绑定具体 grant 和任务范围；开始、恢复、危险执行、文件发送前都重新检查 grant、端点绑定和路由版本。租约覆盖已批准任务而非无限扩权；过期、移除机器人、管理员撤权后暂停，不靠模型自述续期。
   - 没有成员退出事件/查询时，无法保证实时知道某人已退群。界面说明此边界，用有限期限、敏感交付重新证明和管理员撤销控制风险；不能承诺实时成员鉴权。
3. **操作授权沿用既有体验**：
   - 配额内隔离沙盒、写代码、安装依赖、构建、发送请求的文件按现有策略自动执行，不因新平台再逐步确认。
   - 重要宿主机命令由 owner 批准任务范围一次；后续在相同目标、操作范围、期限与版本内豁免重复询问。超范围、过期或撤销仍需新授权。
   - OTP 发往已确认的 C2C 身份，不要求一定是“体验用户”，但须实际可达。发不出或结果不明时取消该挑战并暂停操作；请 owner 私聊/登录可信控制台重新发起，绝不排队到几小时后补发旧码。
4. **历史迁移单独发起**：绑定新群和导入旧群历史是两个动作。管理员预览源 OneBot 账号/群、目标官方账号/群、消息数量、个人记忆、文件、活动任务、定时任务及待投递项，并确认目标群适合读取这些旧资料。不能从群名相同或绑定码推断是同一批成员。
5. **持久迁移状态机**：`planned → frozen → draining → switching → verifying → completed`，失败进入 `rollback_pending / rolled_back`。一次只迁一个测试群，路由有唯一活动端点和递增 epoch。
   - 冻结源群新回合及新出站领取；停旧触发器不等于停旧 worker，必须排空在途任务或安全暂停。
   - 已确认送达的消息不搬、不重发。结果不明的旧尝试仍留在旧路由等待对账，不能换官方渠道再发一次；可先隔离并明确报告未迁项，不假报全部完成。
   - 可续任务迁的是结构化意图和产物引用，不是 CQ 文本、旧 `bot_id`、旧验证码、旧附件句柄或已序列化 HTTP payload。不能批量 SQL 改 target 就算完成。
   - 校验任务操作者映射、项目访问和文件路线后，事务切换 canonical 会话活动端点与 epoch；旧 worker 在发出前发现版本变更须停止。
   - 在新端点确认一条受控文字及文件回执，核对未完成项，再提交完成状态。中途崩溃按日志阶段接管，不重新跑一遍迁移。
6. **历史不等于个人身份**：可经批准复用群公共时间线、章节和公共记忆；每条旧消息保留原平台作者。未证明的新旧个人身份不合并，旧个人记忆不作为新用户事实。owner 授权映射也不默认合并个人记忆。
7. **双通道与回退**：未迁移群照旧 OneBot；已迁移群停止旧入口与旧出站。官方故障不自动回落到可能封控的普通 QQ。回退按第 12 节显式切路由，保留原数据，不靠删除一条标记恢复。

**验收**：新旧身份错配、跨 AppID、同名不同群、项目撤权、过期 OTP、迁移中宕机、旧 worker 在途、已送/未送/结果不明三类投递、提醒/例行/Sub-Agent/项目文件分别续接，以及显式回退。

### M6：文件交付

**执行位置：M4 之后、M5 之前。目标**：至少一条可核验的文件出口先跑通；生成 PDF 不能等于群里收到 PDF。

**内容**：

1. **承接前检查出口**：目标账号/群的原生文件已验证，或已配置接收者可访问的限时下载后端，至少满足一个；同时检查空间、尺寸、权限及额度。两者都没有时提前说明，不能接完任务才第一次发现不能交付。
2. **按内容及请求选择**：图片/视频走已验证媒体类型；PDF/zip 等优先原生文件，明确上传拒绝且尚未发送消息时改下载链接。用户明确要附件，不把小文本粘到聊天里算附件成功；仅允许文本预览时才用代码块。都不可用则展示“生成成功、交付失败”，提供受保护控制台取件，不上传公共网盘。
3. **交付状态**：`generated → stored → uploaded/published → message_accepted`；只有可观测的下载证据才另标 `downloaded`。上传成功不推进消息已送状态，链接已发送也不能说用户已下载。任务验收与附件状态分开，最终总结读取最新状态，消除“不能发送”和“1/1 已送达”同时出现的矛盾。
4. **产物生命周期**：
   - 沙盒销毁前先将产物导入持久交付存储，校验 hash/大小，写入引用；不能只保存 VM 临时路径。允许安全草稿交付，但标注未完成部分，不把失败任务产物冒充验收通过。
   - 当前 `agent/workspaces.py` 是确认交付后按 retention 清理，配置最多一小时。链接默认也设为**一小时**，不能发二十四小时有效链接却一小时删掉文件。
   - 待交付产物由投递引用保留到明确截止时间，受磁盘配额约束；等待队列默认最长二十四小时不代表已交付副本延长保留。截止/撤销后清理并留下失败原因；结果不明也有保留上限，不无限积压。
   - 仅在发送条件就绪时签发链接，记录绝对过期时间；发布对象与沙盒清理分离，到期前不会误删。若用户要求严格“生成后一小时就删”，投递截止同步缩短，明确超时不可交付。
   - 重试只能刷新**确认未发送**的过期链接；结果不明不能重新造链接自动再发。副本最长保留规则以第一次可能被平台接受的发送时间为界，不因每次重试续期。
5. **`FileLinkPublisher` 至少实现一种后端**：
   - S3 兼容对象存储：复用成熟 `botocore` 等库，不自写 SigV4；私有对象、限时签名、受限服务端凭证。签名过期不等于对象已删除，另有清理任务和生命周期兜底。
   - 自建下载服务：可复用 Worker 的存储/回执机制，**不能直接把可执行 HTML 预览当下载服务**。使用独立下载路由/来源、只读文件对象、有效期和访问控制，不暴露主控台会话。
   - 自建 bearer token 至少 128 位随机、数据库存摘要；附件下载、`nosniff`、不缓存、文件名清洗、防目录穿越及 MIME 检测；模型只能引用服务端真实回执，不生成链接。签名 URL 不进普通日志。
   - 链接拿到就可能被转发，不承诺只认原群成员；敏感文件要求登录或明确获准的交付方式。自建令牌可撤销，直接 S3 签名的提前失效依赖删除对象/撤权等后端能力，须如实说明。
6. **接通现有出口**：`project_file_transport.deliver_file`、`agent_tools.send_file_content`、`agent/background.py` 等统一路线与回执；M5 完成项目鉴权后才开启项目文件。OneBot 原上传和核对逻辑不改。`capabilities.file` 与实际路线一致，链接/文本降级明确标注，不冒充原生文件。

**验收**：PDF/zip 原生与链接两条路径；指定目标正确；大文件分片中断；上传成功发送失败；发出后丢回执；任务重启；草稿交付；链接临近过期；真实文件 hash 可核对；已交付一小时清理、未交付截止清理及无引用对象清理。禁止外发测试文件到未配置的公共服务。

### M7：其余功能对齐

按第 9 节逐项处理；先接通既有业务命令，再做可选平台能力。每项分别记录“已实现 / 假服务已测 / 本账号实测”，不能合称全部可用：
- **命令与后台任务**：补齐 `/例行`、`/群百科`、`/memory`、项目命令及任务通知，复用 M5 权限与 M3 投递。工具生成的身份句柄不能跨群借用。
- **OCR**：qq-official 不调用 NapCat 的 `ocr_image`，改用现有的视觉模型路径（`vision_worker.py`、`ocr.py` 里非 NapCat 的部分）。
- **语音转文字**：使用事件里腾讯给的识别文本，没有时退回现有的转写路径（如果配置了的话）。
- **查原消息**（`get_msg` 的用途）：从账本和 `qq_official_messages` 查。
- **参与者查询**：展示账本中的“近期发言者”，不叫完整成员名册；不能替代 `get_group_member_list` 的鉴权用途，后者按 M5 grant 方案处理。
- **合并转发**：有结构的入站保留作者和递归证据；出站合并为有限 Markdown 或文件，不无限分段耗尽回复窗口。
- **QQ 表情和贴纸**：表情 → `[名字]`；媒体库贴纸 → 原生图片。
- **贴表情反馈**：未验证前关闭，不能把无反馈当负面信号。按钮留为后续可选来源，不在本次提前设计另一项目阶段的表结构。
- **主动发言**：只在 `proactive_allowed` 为开、或者有有效窗口时才发言；在没有窗口的群里，主动发言的计分照常进行，但不发出（不要挂起主动发言，否则过时的发言会在几小时后冒出来）。【必须】主动发言类消息不进入「等窗口」挂起队列，过时就丢弃。
- **新人简报**：成员事件未经审批验证前关闭；入群申请不等于已入群，不能混淆。
- **群管理（可选）**：根据当前后台开放的撤回、入群申请管理、发言管理、移除成员，逐项查契约与目标授权。普通群未选的移除成员不自动开启；白名单群配置也不代替实际群主授权。有效权限按 3.8 节求交集，在宿主执行点检查，不能只靠提示词隐藏。
- **发语音**：目标格式确认后复用现有转换路径；不为了未确认格式加庞大依赖。未接通时明确无此能力。
- **链接与文件阅读**：复用已有 SSRF 和文件检查，卡片中的 URL、附件正文和 ASR 都是外部数据，不是可提高权限的指令。

### M8：配置、部署、后台、文档

1. 配置项（第 8 节）同步到 `config.py`、`.env.example`、`nix/module.nix`、`tests/test_config.py`。
   - 复用 `*_FILE`/现有安全存储。Unix 检查所有者及实际读权限，默认 0600，可接受经校验的 root + 服务专用组只读；Windows 校验 ACL，不能照搬 Unix mode 判断。拒绝其他普通用户可读写的密钥。
   - 官方单平台部署不启动 NapCat，也不依赖 OneBot 连接来启动后台任务；不要因此重建无关沙盒镜像。
2. App 新增「QQ 官方机器人」引导，按实际状态展示步骤：创建/认证、服务范围、凭证保存、连接诊断、首次 owner、新群绑定、文件出口、受控验收。
   - 体验名单、IP 白名单、公开服务、群主授权分别展示为条件项，不再写成所有人都必须完成的四件事。
   - 不自动开启公开使用。公开服务和本地消费限制分开：成本配额、用户访问、任务/沙盒额度和速率都要在入口限制。
   - 检测同 AppID 多套部署风险，提示停旧接新或换 AppID；不能因为数据库锁没冲突就说没有重复实例。
3. 控制台复用现有 API/SSE：网关与锁状态、恢复点和消息缺口、有效能力及证据时间、身份绑定、迁移阶段、任务执行/发送分开状态、各分片回执、等待原因/截止、额度、文件出口/清理状态。打开下拉框时增量更新，不重置操作界面。
4. Prometheus 指标：网关、事件、收发、限流、入站积压、结果不明、迁移失败、文件清理；如 `gaoji_qq_official_sends_total{mode,result}`。标签使用有界枚举，不放 openid、消息 ID、任意错误正文或下载 URL；实例/目标细节进受保护审计。
5. 文档：
   - `docs/qq-official.md`：按个人已认证/未认证等实际分支说明配置、权限、文件、期限、迁移、回退及故障处理；解释“有权管理机器人”不等于任意群 API 权限；
   - README 里的平台说明增加一节，指向这份文档；
   - 本方案文档末尾追加「实现记录」，说明和方案不一致的地方。

### M9：全面回归和交付

1. 全套测试、ruff、compileall、`nix flake check`（如果改了 Nix）、`admin-ui` 的 `npm run build`（如果改了前端），全部通过。
2. 测试矩阵包含官方关闭、官方单平台、双平台三种启动形态。已覆盖默认关闭的完整回归无需重复耗时运行，但必须记录具体模式和结果。
3. 自查第 5 节的每一条不变量，在进度记录里逐条写上对应的测试名。
4. 第 11 节交给用户受控执行；未返回结果只标“离线实现完成，真机待验收”，不能算生产可用。测试账号启用和生产切换是两次不同动作。
5. 按 0.4 节完成 PR；合并后默认关闭。用户在独立测试实例开启并验收，确认后才在目标生产部署启用及迁移，不先全量搬群。
6. 用户确认不再需要后，可以删除 worktree（`git worktree remove /Users/kenneth/Desktop/comp390/bot-qq-official`）和远端分支。删除前先问用户。

---

## 7. 数据库迁移

【必须】用 Alembic，放在 `migrations/versions/`，实施和提交前均重新检查迁移链。先扩展兼容 schema，再切业务读取，不破坏旧 OneBot。并行分支产生多个 head 时，经核对建立合并 revision；不能改写已发布/已应用迁移只为“凑单 head”。最终 `python -m src.bot_storage.cli check` 通过、单 head。

优先扩展现有账号、投递、任务与权限存储，下面是必须覆盖的**逻辑记录**，不要求机械创建十三套新子系统：

| 表 | 用途 | 关键字段 |
|---|---|---|
| 账号、端点和身份关联 | 隔离 AppID/环境、群身份/C2C 身份 | account、endpoint、identity_kind、namespace、native ID、principal；有唯一约束，不直接用裸 openid 查旧 ledger identity |
| `qq_official_messages` | 原生消息/引用索引映射 | account、endpoint、message ID、msg_idx/ref_idx、canonical message、方向；索引均包含账号/端点；缺省字段用部分唯一索引 |
| `qq_official_inbound_events` | durable inbox，不只是去重名单 | 事件 ID、消息 ID、session/seq、状态、租约/epoch、回合派发 ID、重试/失败原因；唯一约束保证并发不重复 |
| 网关 session/checkpoint | 安全恢复与失锁停机 | account、环境、session_id、连续 durable seq、fencing epoch、状态和缺口 |
| `qq_official_reply_windows` | 窗口和序号分配 | account、endpoint、凭证类型/值、原始时间、期限、用量、下个 seq；事务关联发送尝试 |
| 现有 delivery/attempt 扩展 | 分片、发送意图与回执 | 逻辑投递、fragment ID、payload hash、路由 epoch、窗口/seq、结果、平台回执；所有出口复用 |
| 配额计数 | 跨重启保留每日/周期额度 | account、场景、端点、周期起止、已用/保留量、服务端拒绝与下次可试时间 |
| 群状态及 capability evidence | 授权按目标管理 | account + group、能力状态、证据、revision、观察/过期时间、入/退群时间；不以裸 group_openid 作全局主键 |
| binding challenges / owner links | 首次身份与双向绑定 | challenge ID、HMAC、创建者、候选群及 actor、尝试计数、期限、确认者、消费状态；每个账号独立 |
| 项目/任务 grant 扩展 | 代替错误的历史成员鉴权 | account、actor、项目、任务、能力、证据、期限、撤权版本；保留原 OneBot 验证分支 |
| 会话路由及 migration journal | 一个活动端点、可恢复迁移 | 源/目标、canonical ID、route epoch、阶段、未决尝试、授权、确认记录；活动映射有唯一约束 |
| `file_links` / artifact references | 文件路线、到期和清理 | account、endpoint、actor、artifact hash、对象位置、类型/大小、token 摘要、消息回执、expires/purge 时间、引用计数 |

原始 inbox payload 按隐私和恢复需要限时保存；去重墓碑、已处理消息索引与派发唯一键不得随意七天一删导致恢复后再次触发。明确数据保留策略，清理只删除不再需要的 payload，不删除仍有任务引用的证据。session、绑定码和发送凭证不进入普通管理列表。

迁移在空库和带旧数据的测试库上验收，检查唯一键、并发锁、幂等和历史查询。保留 schema 与数据，不用生产 downgrade 作为常规回退。

---

## 8. 配置项

### 8.1 当前已接入配置（M2/M3）

以下变量已由 `adapters/qq_official/config.py` 读取，并已接入 App 启停。默认关闭时不读取官方凭证或解析休眠配置；开启后仍需通过账号租约、绑定和当前准入策略。当前 runtime 已持久接收入站与回合意图，并接通 M3 发送循环；M4 已接通共享模型回合、首批命令和文字 Sub-Agent，并通过完整离线回归；真实 `/help` 已由用户确认。M3 的等待/渲染等构造参数已受校验，尚未新增下表以外的环境变量。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AI_QQ_OFFICIAL_ENABLED` | `false` | 总开关 |
| `AI_QQ_OFFICIAL_APP_ID_FILE` | 空 | 必填受保护 AppID 文件，单个文本值；不把内容写进 Git/Nix store |
| `AI_QQ_OFFICIAL_APP_SECRET_FILE` | 空 | 必填受保护 AppSecret 文件；有效 ACL 与文件权限必须通过运行时验证 |
| `AI_QQ_OFFICIAL_BIND_PEPPER_FILE` | 空 | 必填独立 HMAC pepper 文件：32 个安全随机字节编码为 64 个十六进制字符，不复用 AppSecret |
| `AI_QQ_OFFICIAL_API_BASE` | 空 | 空值随环境选择协议默认地址；生产默认 `https://api.bot.qq.com`。覆盖须通过可信域和环境检查，不静默跨环境 |
| `AI_QQ_OFFICIAL_SANDBOX` | `false` | 沙箱地址按契约配置；环境参与全部 key，不静默切换 |
| `AI_QQ_OFFICIAL_ACCOUNT_KEY` | 空 | 可选规范 AccountKey 校验值；设置时必须与文件中的 AppID 和环境完全一致。实际 key 始终由已校验账号生成 |
| `AI_QQ_OFFICIAL_OWNER_OPENIDS` | 空 | 当前账号下的 C2C owner，最多 32 个；可信配置初始化，空则只能进行受限引导 |
| `AI_QQ_OFFICIAL_PRIVATE_POLICY` | `owner_only` | 可选 `owner_only / allowlist / public`，与腾讯服务范围分开 |
| `AI_QQ_OFFICIAL_PRIVATE_ALLOWED_OPENIDS` | 空 | allowlist 的本账号 C2C 身份，最多 256 个；不授予管理权限 |
| `AI_QQ_OFFICIAL_GROUP_ADMISSION` | `manual_bind` | 即使腾讯允许公开加群，本地仍须绑定；v1 不自动启用任意群 |
| `AI_QQ_OFFICIAL_INBOX_PAYLOAD_TTL_HOURS` | `24` | 范围 1–168；限制原始 payload 保留时间，待处理事件先终态过期再清理，不删除影响恢复的去重/索引/派发记录 |

身份列表推荐使用 JSON 字符串数组，完整保留非数字 opaque ID；也兼容逗号分隔配置。账号、环境、群成员身份与 C2C 身份不因昵称或字面 ID 相同而合并。私聊准入不代替 owner 授权，首次私聊不能认领 owner。

三个文件均须通过权限和有效 ACL 检查。通常使用运行服务用户或 root 所有的 `0600` 文件；代码仅在专用服务组与 ACL 验证均通过时接受受限的 root/服务组读取。不能因为 `chmod 0600` 就忽略额外 ACL 授权，也不能让应用读取权限不明的文件。关闭开关时不读取这些文件。

NixOS 对应 `services.gaoji.qqOfficial`：`enable`、`appIdFile`、`appSecretFile`、`bindPepperFile`、`apiBase`、`sandbox`、`accountKey`、`ownerOpenids`、`privatePolicy`、`privateAllowedOpenids`、`groupAdmission`、`inboxPayloadTtlHours`。默认不增加官方环境变量或 credential 项；启用后使用 `LoadCredential`，应用读取 `/run/credentials/gaoji.service/qq-official-{app-id,app-secret,bind-pepper}`。文件选项必须是 Nix store 外的绝对字符串，不使用 Nix path literal、`builtins.readFile` 或内联秘密。身份列表作为普通 Nix 配置会进入 Nix store；如需隐藏这些身份配置，使用既有受保护环境文件入口。

TODO(qq-official)：Nix 求值检查不验证目标系统上 systemd credential 的文件系统与有效 ACL；M8 必须实测，不能放宽安全规则。当前沙箱 API 地址对目标账号的可用性也仍待验收。

### 8.2 后续里程碑配置（尚未接入，不因出现在本表而生效）

| 变量 | 计划默认值 | 说明 |
|---|---|---|
| `AI_QQ_OFFICIAL_NATIVE_FILES` | `false` | 群/私聊分项实测后允许；端点有效能力继续限制 |
| `AI_QQ_OFFICIAL_MARKDOWN` | `true` | 能力待验证；仅明确格式拒绝且未发送时降级 |
| `AI_QQ_OFFICIAL_MAX_TEXT_CHARS` | `2000` | 单条长度上限，按实测结果调整 |
| `AI_QQ_OFFICIAL_DEFER_MAX_HOURS` | `24` | 持久任务结果等待上限；不适用于过时插话和 OTP，受产物截止限制 |
| `AI_QQ_OFFICIAL_RATE_*` | 第 3.5 节分类数值 | 按认证、群/C2C、主动/被动区分；真实限流优先 |
| `AI_QQ_OFFICIAL_PROJECT_GRANT_TTL_SECONDS` | 保守有限值，实施时确定 | 配合已有项目/任务授权；不自动续权，不代替成员证据 |
| `AI_FILE_LINK_BACKEND` | `none` | `none / s3 / download`；下载服务不是 HTML 预览 |
| `AI_FILE_LINK_TTL_HOURS` | `1` | 默认一小时；不得超过产物实际可保留期限 |
| `AI_FILE_LINK_S3_ENDPOINT`、`_BUCKET`、`_REGION`、`_ACCESS_KEY_FILE`、`_SECRET_KEY_FILE` | 空 | S3 兼容存储的设置 |

【自行判断】名字可以调整，但要和现有配置的风格一致，并且全部写进 `.env.example` 和 `nix/module.nix`。

新增功能配置不打开腾讯公开服务/群权限开关，也不创建全局服务器管理权限。上线前校验：文件路线至少一个可用、绑定 owner 存在、端点范围明确、保留/链接/等待期限无冲突。

---

## 9. 功能对照表（目标行为，不是已完成清单）

| 功能 | onebot-v11（NapCat） | qq-official |
|---|---|---|
| @ 回复、引用续聊、私聊 | 已有 | M4；按平台服务范围和本地策略，不写死仅体验用户 |
| 收全部群消息（上下文、Historian、群百科） | 已有 | 目标群授权且事件实测通过后接入，不自动启动主动发言 |
| 主动发言 | ✅ | 群主开「主动发言」时可用；没开时只在有窗口时发，过时就丢弃 |
| Sub-Agent、例行任务、提醒、项目通知 | 已有 | M4/M5/M7；持久队列、授权和投递状态分开；无合法窗口时有界等待 |
| Markdown | 按既有渲染 | 格式分别实测；明确格式拒绝才降级 |
| 收图片、语音、视频、文件 | 已有 | 以实际附件字段为准；腾讯 ASR 缺失时不能假装听过 |
| 发图片、视频 | 已有 | M6 按目标账号/端点实测上传与发送 |
| 发语音 | 已有 | 可选，编码和限额验证后接通 |
| 发文件 | 已有原生上传 | 原生文件（待实测）→ 下载链接；文本预览不冒充文件交付 |
| 群文件区（列表、下载） | 已有 | 当前不依赖，未支持工具隐藏 |
| QQ 表情 | 已有 | 收：能解析时保留名字；发：文本降级 |
| 贴纸（媒体库） | 已有 | 按图片发 |
| 贴表情（收和发） | 已有 | 当前不依赖，以后可验证原生能力或按钮反馈 |
| 合并转发 | 已有 | 收：保留结构化作者/引用；发：有限 Markdown 或文件，不冒充原生转发 |
| 群成员列表 | 已有 | 仅近期发言者视图，不用于证明当前成员资格 |
| 群名片 | 已有 | 不宣称可获取，有昵称则保留昵称 |
| NapCat OCR、语音转文字 | 已有 | 自有视觉模型、腾讯识别文本或已配置转写 |
| 撤回 | 已有 | 自身/他人分别验证时限和群有效授权，不能只看本地管理员身份 |
| 知道别人撤回了消息 | 已有 | 未验证通知前不承诺可追踪 |
| 新人入群简报 | 已有 | 成员事件未验证时关闭，不把申请进群当已加入 |
| 撤回/入群管理/禁言/移除成员 | 依现有权限 | M7 可选，平台契约、开发者选项、群主授权、本地权限共同决定 |
| 沙盒、浏览器、模型 | 已有 | 复用内核，适配 ActorKey 和任务权限后接通 |
| 项目空间、记忆、文件 | 已有 | M5/M6；项目访问与个人记忆隔离不可省略 |
| 管理员和验证码 | 既有 QQ 身份 | M2 首次可信关联，M5 任务级授权；普通操作不反复验证 |

`OutboundCapabilities` 根据实现与端点验收生成，不能写死全为 native。可用时 mention/image/video 为 native，emote/card 为 text，贴纸走图片，audio/file 依实际路线；多媒体分片计入窗口。`max_text_bytes` 与字符数是不同限制，UTF-8 编码后再检查，不能把 2000 字直接当 2000 字节。

---

## 10. 测试策略

### 10.1 命令

```bash
PYTHON_DOTENV_DISABLED=1 python -m unittest discover -s tests -p 'test_*.py'
ruff check bot.py src tests tools migrations packaging/server nix
python -m compileall -q bot.py src migrations tests
python -m src.bot_storage.cli upgrade
python -m src.bot_storage.cli check
```

本地需要 PostgreSQL（带 pgvector），`AI_POSTGRES_DSN` 和 `TEST_POSTGRES_DSN` 指向两个不同的库，要求见 README 的测试一节和 `.github/workflows/ci.yml`。【必须】不要连接生产库。

### 10.2 要求

1. **离线测试**：腾讯端点全部连假服务，LLM 用 fake。只允许声明的本机测试服务/PostgreSQL，禁止外网与生产 DSN；不能为跑测试读取真实密钥。
2. **假时钟**：窗口、额度、token 过期、重连退避、挂起超时，全部用注入的时钟测试，不调用真正的 `sleep`。
3. **并发**：窗口争用、同一会话的发送顺序、token 只换一次、重复事件并发到达，都要有并发测试（`asyncio.gather` 多个协程）。
4. **表驱动**：错误码映射、事件解析、Markdown 渲染都用表驱动测试，每加一种情况就加一行。
5. **夹具**：M0 探测工具产出的脱敏事件样本放进 `tests/fixtures/qq_official/`，加上按第 3.4 节手写的样本。
6. **回归**：M1 的 OneBot 出站快照测试一直保留，以后每次改动都要跑。
7. **边界扫描**：协议库不 import gaojibot/nonebot，公共端口不 import OneBot；官方调用链不能落到 legacy Event 分支。
8. **秘密与身份**：假 AppSecret、token、验证码、签名 URL 不泄露；同名用户、跨群、跨 AppID、沙箱/生产相同字面 ID 均不能串用。
9. **迁移**：空库与旧库 upgrade；真实测试 PostgreSQL 验证唯一键/事务/锁；群历史可连续、个人记忆不自动合并、迁移恢复/回退不会重发。
10. **风险驱动覆盖**：协议解析用小型表驱动测试；身份、窗口、投递、项目鉴权、迁移和文件清理必须覆盖关键分支和宕机边界。不要为文案/纯配置小改重复全量重测，里程碑再做整体回归。
11. **注入**：入站文字里手打 `<@xxx>`，不能被当成 @；模型输出里夹带 `<qqbot-at-user …/>`、`<faceType=…>`，发出去时必须被转义；模型编造的 mention 句柄不能变成真的 @。
12. **实例租约**：同数据库两实例互斥；锁连接掉线、旧 worker 仍运行、epoch 过期时不再派发。独立数据库不能互斥的限制须在 App 诊断中说明，不写一个假测试宣称可以。
13. **入站崩溃点**：接收未入库、入库未派发、派发未执行、执行后重放分别恢复；checkpoint 不跳过未持久化事件。
14. **出站崩溃点**：计划生成、意图写入、HTTP 发出、平台接受、响应到达、回执提交各处中断；不能出现已送分片从头发送或不明尝试被 `defer_unsent` 清除。
15. **鉴权攻击面**：第一个访客抢 owner、公开绑定码抢用、跨群使用 grant、旧授权迁移、历史发言者伪装成员、OTP 延迟送达都必须拒绝；正常已授权任务不反复询问。
16. **文件时间线**：长任务超过回复窗口、上传后无窗口、文件发送后响应丢失、链接临期、存储满、重启后清理、沙盒已销毁等组合；链接有效期间不能 404，过期后副本可被实际清理。
17. **官方单平台启动**：未加载 OneBot/NapCat 时也能接消息和投递；官方关闭无业务副作用；混合模式未迁移群行为不变。

---

## 11. 用户真机验收清单（执行者整理好交给用户）

准备：独立测试会话、受保护凭证、已核对服务范围。体验名单/IP 白名单按实际需要配置；群主按要测试的能力授权。公开服务不默认开启。用户执行探测时只授权列明的测试目标，不操作生产服务器或真实群成员。

1. 运行 M0 的探测工具，把报告结论回填到第 3 节，设置好开关。
2. 通过可信控制台配置自己的 C2C owner，执行 M2 的双向新群绑定；用第二个普通身份确认无法抢占 owner，且正常聊天不重复绑定。
3. 先用新会话验证文字和文件，再用脱敏旧记录/测试群验收 M5 迁移；不要第一步把生产全部搬过去。
4. 群里不 @ 闲聊几句，再 @ 提问：能读取刚才的连续原文，不会因为旧消息曾 @ 过机器人就抢走当前话题。
5. 引用机器人的一条回复继续追问：能接上。
6. 发图片、语音：能识别。
7. 表格、代码块、公式分别验证，不能只看消息成功就判渲染正确；每组使用新窗口。
8. 交给 Sub-Agent 一个超过 5 分钟的任务：
   - 打开「主动发言」时，完成后直接发到群里；
   - 关掉时先挂起；出现新的**合法**回复窗口后送达。普通非 @ 消息是否能供窗口使用必须实测，不能预先保证随便说一句都行。
9. 交付 PDF 和 zip，核对实际下载内容/哈希，不只看“已上传”；确认一小时链接到期、对应副本清理，已确认交付不会因重启再发。
10. 测提醒和例行任务的发送/等待状态；过时插话不补发，等待到期可在控制台看到明确原因。
11. 在无害测试操作上验证任务级授权：owner 一次批准后范围内继续，普通成员被拒；撤权/超范围需新批准。真实重启、删文件、禁言、踢人不属于默认验收。
12. 仅中断测试进程的连接或重启测试实例，不修改主机网络；验证可恢复事件不重复，无法恢复的缺口能显示，结果不明不自动再发。
13. 验证官方单平台与双平台；未迁移 OneBot 群不受影响，已迁移测试群旧入口/出站停止；执行一次显式回退。
14. 项目任务恢复、权限过期、文件发送前撤权分别测试；确认“近期发言者”不会被误当成完整成员列表。

记录版本、环境、能力范围、时间与脱敏证据。未测试项标“待验收”；可选能力未通过则关闭，不谎称全部可用。

---

## 12. 风险和回退

| 风险 | 应对 |
|---|---|
| M1 改坏了 OneBot | 出站快照测试；每步全绿才提交；每个 PR 等 CI 通过才合并；合并后发现问题就撤销那个 PR（见下面的回退） |
| 合并时和另一个会话在 `main` 上的改动冲突 | 每次推送前先合并 `origin/main` 并重跑测试（0.4 节）；冲突在分支上解决，不在 `main` 上解决 |
| 腾讯接口行为和文档不一致 | 【待实测】项全部双分支处理；M0 探测工具先测清 |
| 平台审核/账号受限 | 明确违规不重试；保留原码与证据，按官方流程处理，不承诺不会封控或一定解封 |
| 认证后可公开服务，但目标群没授权 | 分开展示服务范围、本地策略、群授权与已验证能力；不把开发者管理员身份当万能权限 |
| 无全量消息或主动窗口 | 上下文/定时交付能力显式降级；任务结果有界等待，不承诺任何时候立即送达 |
| 多套独立部署使用同 AppID | 同库锁不解决跨库冲突；不同 AppID 或明确停旧接新 |
| 项目误用历史成员列表 | M5 独立 grant、有限期限、敏感发送前复核，不以旧发言推断实时成员 |
| 结果不明却重复补发 | 发送尝试持久化、真实回执对账；没有查询能力时明确待核实 |
| 下载链接泄露/提前失效 | 一小时、访问控制、附件响应、存储引用；承认 bearer 可转发，敏感内容用登录保护 |
| 文件清理与挂起投递冲突 | 统一绝对期限和容量预算，未交付/已交付分开，过期失败明确显示 |
| 数据库并行迁移冲突 | 核对 DAG，合并 revision；不改写已应用的迁移 |

**回退**分三种情况：

1. **停用或退回 OneBot**：未迁移时可停用官方运行时，保留数据。已迁移时先冻结官方新出站，排空/隔离在途与结果不明项，核实旧 OneBot 在线且有目标权限；显式审批回退计划，事务切活动端点及 epoch，做受控回执检查后恢复旧入口。不删除一行标记就贸然开双路发送；旧 QQ 已封控时保持暂停并说明原因，不能自动回退。
2. **某个已经合并的 PR 有问题**：在 GitHub 上打开那个 PR，点「Revert」，会生成一个撤销用的新 PR，检查后合并。也可以在分支上执行 `git revert -m 1 <合并提交>`，再按 0.4 节走 PR。不要直接改写 `main` 的历史。
3. **需要看或者恢复改造前的版本**：`pre-qq-official` 标签永久指向改造前的 `main`，可以从它重新开分支。

---

## 13. 完成定义

分成两个层次记录，不能把“写完代码”当“已上线”。

**离线实现完成**：

- [ ] M0–M9 全部完成，每个里程碑有提交和进度记录；
- [ ] 全套测试、ruff、compileall、迁移检查通过；官方关闭且未迁移的既有会话行为不变；
- [ ] 第 5 节全部不变量都有对应测试，未解决项有明确阻断/降级；
- [ ] 第 3 节待实测项有未知状态、失败路径、可恢复开关，不自行扩大授权；
- [ ] `docs/qq-official.md`、`docs/qq-official-probe.md`、`.env.example`、`nix/module.nix` 已更新；
- [ ] 真机验收清单已整理好交给用户；
- [ ] 已推送 `pre-qq-official` 标签，改造前的版本有保留；
- [ ] 改动按 0.4 节 PR 合并，保留作者历史；默认关闭，无强推、无无关改动；
- [ ] 执行者开发/CI 未使用真实凭证，未把真实身份或凭证写入仓库。

**受控验收后可启用**：

- [ ] 用户完成目标账号/测试群文字与上下文验收，能力状态带实测证据；
- [ ] 至少一个文件出口实际可下载，PDF/zip 内容核验通过，清理期限正确；
- [ ] 重启恢复、重复抑制、窗口等待、结果不明、任务级授权和项目隔离通过；
- [ ] 用测试群完成历史迁移及回退，未迁移的 OneBot 群不受影响；
- [ ] 管理员批准目标范围的生产切换；未验证的可选能力继续关闭。

### 本次修订记录（2026-10-28，仅文档）

1. 根据认证后的后台修正公开服务、五百群上限与群管理授权的描述，区分页面配置、接口文档、真实验收。
2. 新会话身份绑定提前到 M2，文件交付提前到历史迁移之前；先小闭环，再搬旧数据。
3. 补账号/环境隔离、持久 inbox、分片回执、失锁停止和跨数据库实例限制。
4. 项目鉴权不再依赖“历史发言者就是成员”；历史迁移不再直接重写投递目标或继承旧授权。
5. 下载链接默认一小时，区分未交付产物、已交付副本和沙盒生命周期。
6. 补迁移状态机、显式回退和离线/真机两阶段验收。本次未实现、未提交推送、未部署。

### 实现记录（2026-10-28）

- 已按用户的执行请求建立独立 `feat/qq-official` worktree，保存并推送改造前标签。
- M0 使用已有 httpx/aiohttp，不新增依赖；协议包与应用运行时分开。实际完成项、测试、PR 及限制统一记录在 `docs/qq-official-progress.md`。
- 完整旧基线发现两处已有阻断：controller 镜像未包含传递依赖、Alembic 禁用了已有 logger。最小修复分别补 COPY 与保留 logger，未构建或部署镜像。
- M1 的 OneBot 快照与依赖核对提前准备，尚未据此宣称业务入口已经解耦。
- Unix 凭证和探测报告额外检查有效 ACL；无法核实的文件系统拒绝使用。Windows 原生 ACL、Linux 不支持 ACL 查询的 tmpfs 兼容性留待 M8，未声称所有系统可用。
