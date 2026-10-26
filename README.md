<p align="center">
  <img src="docs/assets/gaoji-banner.svg" width="100%" alt="gaoji - QQ multi-model agent">
</p>

<h1 align="center">gaoji</h1>

<p align="center">
  一个能理解群聊上下文、调用工具并完成真实任务的 QQ 多模型 Agent
</p>

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/version-0.20.0-22c55e?style=for-the-badge">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-18181b?style=for-the-badge"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776ab?style=for-the-badge&amp;logo=python&amp;logoColor=white">
  <img alt="NoneBot2" src="https://img.shields.io/badge/NoneBot2-OneBot_V11-ea5252?style=for-the-badge">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-Durable-4169e1?style=for-the-badge&amp;logo=postgresql&amp;logoColor=white">
  <img alt="NixOS" src="https://img.shields.io/badge/NixOS-Reproducible-5277c3?style=for-the-badge&amp;logo=nixos&amp;logoColor=white">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#系统结构">系统结构</a> ·
  <a href="#常用命令">命令</a> ·
  <a href="#管理控制台">控制台</a> ·
  <a href="#nix-与-nixos">NixOS</a>
</p>

---

gaoji 通过 NapCatQQ 接收 OneBot V11 事件，使用 NoneBot2 处理消息，并把对话、
工具调用、长期记忆、媒体任务与投递状态保存到 PostgreSQL。它不只是聊天接口：模型会在
宿主管控的 Agent Loop 中读取证据、选择工具、执行任务，再把适合 QQ 的结果发回群聊。

## 主要能力

<table>
  <tr>
    <td width="50%"><strong>多模型路由</strong><br>兼容 OpenAI Chat 与 Anthropic Messages；按群、用户选择模型 profile。</td>
    <td width="50%"><strong>上下文与记忆</strong><br>按模型窗口保留连续原文时间线，旧消息进入 P1/P2/P3 分层章节；群聊、私聊、个人记忆严格隔离。</td>
  </tr>
  <tr>
    <td><strong>可靠 Agent 内核</strong><br>工具风险、显式批准、幂等去重、硬超时、取消补偿、持久接管与逐步审计回放。</td>
    <td><strong>图片、表情与语音</strong><br>临时识图、全局安全表情库、QQ 语音转写与腾讯 SILK 语音回复。</td>
  </tr>
  <tr>
    <td><strong>帖子与视频</strong><br>读取分享正文和评论；B 站链接与 QQ 原生视频支持抽帧、Whisper 转写与视觉综合分析。</td>
    <td><strong>代码沙箱</strong><br>在临时 Docker 容器中创建项目、安装依赖、测试、打包并把产物发回 QQ。</td>
  </tr>
  <tr>
    <td><strong>Durable Runtime</strong><br>PostgreSQL 保存消息、回合、工具效果、Outbox、提醒、持久任务和媒体元数据。</td>
    <td><strong>生产部署</strong><br>实时管理控制台、权威告警查询、Nix Flake、NixOS module、systemd Worker 与数据库迁移。</td>
  </tr>
  <tr>
    <td><strong>集群只读控制面</strong><br>独立服务查询获准节点、systemd 状态与有限日志，保留来源、时间、过期状态和审计投影。</td>
    <td><strong>双层权限边界</strong><br>会话、主机与服务先由 gaoji 收窄，再由运维后端复核；模型不能提交地址、凭据或任意命令。</td>
  </tr>
  <tr>
    <td><strong>实验式排障</strong><br>六类固定流程组合 DNS、HTTP、服务、Trace、Outbox 与数据库证据，最多两层六项检查。</td>
    <td><strong>可追溯结论</strong><br>每次调查生成 diagnostic# 与 evidence#；失败、异常迹象和证据不足分开表达，控制台可逐项查看。</td>
  </tr>
  <tr>
    <td><strong>受控操作合同</strong><br>操作目标、操作者、期限、预期状态和验收方式写入不可变合同；写后端未获批准时明确保持不可执行。</td>
    <td><strong>隔离集群 Worker</strong><br>固定任务模板、独立凭据、资源预留、租约与 fencing；支持产物校验、PDF/媒体检查和限时静态预览。</td>
  </tr>
  <tr>
    <td><strong>借用调度与恢复</strong><br>资源所有者可随时让路；限时授权约束用户、会话、CPU、内存、GPU 与预算，完成检查点可在租约失效后安全接续。</td>
    <td><strong>故障记忆与守护</strong><br>事故串联探测和证据，BGE-M3 检索已验证案例并复核适用条件；固定探针健康路径不调用模型。</td>
  </tr>
  <tr>
    <td><strong>可靠文件交付</strong><br>QQ 群文件真实可见后才确认成功并清理沙盒；回执不明时保留工作区与不可变产物快照供核对和补发。</td>
    <td><strong>实时资源控制</strong><br>控制台统一管理 Worker 状态、资源上限、借用授权、守护合同、故障事件和运行手册案例。</td>
  </tr>
  <tr>
    <td><strong>固定版本部署</strong><br>先在隔离 worktree 校验精确 Git 提交和 flake.lock，再展示 Nix 闭包差异并等待管理员批准。</td>
    <td><strong>分批切换与恢复</strong><br>独立部署器支持串行、金丝雀、逐节点验收和受限回滚；租约、fencing 与主机锁阻止重复副作用。</td>
  </tr>
</table>

## 系统结构

```mermaid
flowchart LR
    QQ[QQ Client] --> NC[NapCatQQ]
    NC -->|OneBot V11 WS| NB[NoneBot2 Gateway]

    subgraph Runtime[gaoji Runtime]
        NB --> IR[Message IR + Ledger]
        IR --> CTX[Context Planner]
        CTX --> AGENT[Agent Loop]
        AGENT --> LLM[Multi-model Gateway]
        LLM --> AGENT
        AGENT --> TOOLS[Tool Policy + Executor]
        TOOLS --> MEDIA[Vision / Voice / Video]
        TOOLS --> BOX[Docker / Browser]
        AGENT --> OUT[Output Planner + Outbox]
    end

    IR --> PG[(PostgreSQL)]
    CTX --> PG
    TOOLS --> PG
    OUT --> PG
    OUT --> NC
    TOOLS --> CC[Cluster Control]
    CC --> OPS[Read-only Operations API]
    CC --> QUEUE[(Operation / Job Ledger)]
    CC --> MEMORY[Incident / Runbook / Guardian]
    CC --> DEPLOY[Immutable Deployment Contracts]
    CW[Isolated Cluster Worker] -->|Heartbeat / Lease / Receipt| CC
    CC -->|Assigned artifact| CW
    CW --> PREVIEW[Expiring Preview Origin]
    CD[Independent Nix Deployer] -->|Lease / Evidence / Receipt| DEPLOY
    DEPLOY -->|Approved Exact Closure| CD

    classDef core fill:#18181b,stroke:#22c55e,color:#fafafa,stroke-width:2px;
    classDef service fill:#27272a,stroke:#71717a,color:#fafafa;
    classDef data fill:#172554,stroke:#60a5fa,color:#eff6ff;
    class AGENT,LLM core;
    class NC,NB,IR,CTX,TOOLS,MEDIA,BOX,OUT,CC,OPS,CW,PREVIEW,MEMORY,DEPLOY,CD service;
    class PG,QUEUE data;
```

模型不会直接操作 NapCat 或数据库。消息先转换成统一的 Message IR，Agent 只能调用宿主
明确提供且经过 JSON Schema 与风险策略校验的工具；危险动作必须来自用户当前消息的明确
授权，非幂等调用会去重，长命令可交给租约队列跨重启执行。最终输出再由宿主降级为
OneBot 消息段并发送。

## 技术栈

| 层级 | 实现 |
| --- | --- |
| QQ 接入 | NapCatQQ、OneBot V11 |
| Bot 框架 | NoneBot2、FastAPI、Uvicorn |
| 模型网关 | OpenAI Python SDK、HTTPX、自定义 Anthropic 协议适配 |
| 数据库 | PostgreSQL、Alembic，可选 pgvector |
| 浏览器 | Playwright、Chromium |
| 沙箱 | Docker |
| 图片理解 | 可配置视觉模型 profile |
| 视频分析 | Bilibili 公共接口、QQ 媒体流、FFmpeg、whisper.cpp |
| 语音 | Edge TTS、腾讯 SILK、NapCat 语音转写 |
| 富文本 | CodeSnap、Pygments |
| 部署 | Nix Flakes、NixOS、systemd、固定提交与闭包验收 |
| 集群控制 | 独立控制服务、只读运维 API、操作合同、资源租约、隔离 Worker、受限部署器、Prometheus 指标 |

## 目录结构

```text
bot/
├── bot.py                       # NoneBot 启动入口
├── pyproject.toml               # Python 项目与依赖声明
├── uv.lock                      # 可复现依赖锁
├── flake.nix                    # Nix 包、开发环境和模块导出
├── nix/module.nix               # Bot NixOS 服务模块
├── nix/cluster-control.nix      # 集群控制服务模块
├── nix/cluster-worker.nix       # 隔离 Worker 模块
├── nix/cluster-deployer.nix     # 固定合同 Nix 部署器模块
├── migrations/                  # PostgreSQL / Alembic 迁移
├── admin-ui/                    # React + TypeScript 管理控制台
├── skills/                      # Agent 按需加载的操作说明
├── docs/                        # 架构、运维和迁移文档
├── tests/                       # 单元测试与集成测试
└── src/
    ├── cluster_control/         # 控制服务、权限、操作合同、队列和产物登记
    ├── cluster_worker/          # 固定计算模板、租约回执和静态预览
    ├── cluster_deployer/        # Git/Nix/SSH 固定部署流程与节点验收
    ├── bot_storage/             # PostgreSQL、迁移与存储工具
    └── plugins/ai_chat/
        ├── __init__.py          # NoneBot Matcher 与兼容入口
        ├── runtime.py           # Composition Root 与服务生命周期
        ├── command_handlers.py  # 命令解析与控制命令
        ├── message_ingest.py    # 消息入库与群上下文采集
        ├── trigger_service.py   # 主动触发与后台认知任务
        ├── chat_orchestrator.py # QQ 对话上下文与回合入口
        ├── tool_executor.py     # Agent 工具循环与执行编排
        ├── reply_service.py     # 回复规划、渲染与安全发送
        ├── onebot_delivery.py   # OneBot Outbox 与提醒投递
        ├── adapters/            # OneBot 等平台事件适配
        ├── application/         # 对话回合与业务用例编排
        ├── agent/               # Agent Loop 对外契约
        ├── tools/               # 工具能力目录边界
        ├── storage/             # 持久任务等应用存储
        ├── workers/             # 可恢复后台任务执行器
        ├── deepseek.py          # Agent Loop 兼容实现
        ├── llm_gateway.py       # 多协议模型网关
        ├── context_pipeline/    # 引用图、话题识别、混合召回与 Token 预算
        ├── turn_journal.py      # 持久回合、工具事件和连续任务
        ├── delivery.py          # 幂等消息投递 Outbox
        ├── media_library.py     # 永久表情库
        ├── vision_worker.py     # 一次性图片理解任务
        ├── video.py             # QQ 原生视频引用与短期缓存
        ├── video_analysis.py    # B 站与 QQ 视频深度分析
        ├── sandbox.py           # Docker 任务沙箱
        ├── browser_tools.py     # 持久浏览器工具
        ├── output_planner.py    # 回复拆分与控制句柄
        └── admin.py             # 管理控制台 API
```

## 运行要求

你需要准备：

1. Python 3.12，或启用了 Flakes 的 Nix。
2. PostgreSQL 服务器。
3. 至少一个可用的大模型 API Key。
4. 已登录 QQ 的 NapCatQQ。
5. 可选的 Docker、Chromium、FFmpeg 和 whisper.cpp。

生产模式必须连接 PostgreSQL，不会静默退回 SQLite。

## 快速开始

> [!IMPORTANT]
> 生产模式必须连接 PostgreSQL，不会静默退回 SQLite。API Key、数据库 DSN 和管理
> Token 只能放在 `.env` 或服务器密钥文件中，不能提交到 Git。

### 1. 安装依赖

```bash
git clone https://github.com/zty20040403/gaojibot.git
cd gaojibot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

也可以使用 Nix 开发环境：

```bash
nix develop
```

### 2. 准备 PostgreSQL

下面是只适合本地开发的 Docker 示例：

```bash
docker run -d \
  --name gaoji-postgres \
  -e POSTGRES_USER=qq_bot \
  -e POSTGRES_PASSWORD=change-me \
  -e POSTGRES_DB=qq_bot \
  -p 5432:5432 \
  postgres:17
```

在 `.env` 中配置连接：

```dotenv
AI_POSTGRES_DSN=postgresql://qq_bot:change-me@127.0.0.1:5432/qq_bot
AI_POSTGRES_SCHEMA=qq_bot
```

初始化数据库：

```bash
python -m src.bot_storage.cli upgrade
python -m src.bot_storage.cli check
```

### 3. 配置模型

最小 DeepSeek 配置：

```dotenv
DEEPSEEK_API_KEY=replace-with-your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
AI_MODEL_DEFAULT_PROFILE=deepseek
AI_SIMPLE_CHAT_PROFILE=qwen-local
```

多模型配置使用一个 JSON 目录。Key 只通过环境变量引用，不要写进 JSON：

```dotenv
DEEPSEEK_API_KEY=replace-with-your-deepseek-key
OPENAI_API_KEY=replace-with-your-openai-key
AI_MODEL_PROFILES_JSON={"default":"deepseek","profiles":{"deepseek":{"provider":"deepseek","protocol":"openai-chat","base_url":"https://api.deepseek.com","api_key_env":"DEEPSEEK_API_KEY","model":"deepseek-v4-flash","aliases":["ds"],"fallback_profiles":["openai"]},"openai":{"provider":"openai","protocol":"openai-chat","base_url":"https://api.openai.com/v1","api_key_env":"OPENAI_API_KEY","model":"gpt-5-mini","capabilities":{"vision":true},"aliases":["gpt"]}}}
```

`AI_SIMPLE_CHAT_PROFILE` 只接管没有显式模型覆盖、媒体输入、强制工具或
Sub-Agent 委派的普通聊天；复杂任务仍使用会话原本选择的模型。

当首选模型超时、断网、限流、欠费或密钥失效时，网关会在同一轮 Agent
中切到兼容的备用模型，不会重复已经完成的工具操作。连续失败会临时熔断；
到达冷却时间后自动进行一次恢复探测。`fallback_profiles` 决定优先顺序，
其余已配置且能力匹配的模型会作为后备候选。模型健康状态可在管理台查看。

本地千问另有独立健康探测：后台检查 `/v1/models` 是否包含目标模型，
不可用、模型未加载或健康信息过期时直接跳过，不让每条聊天等待超时。
控制台「模型与群友」显示服务状态、显存和请求统计；Trace 显示期望模型、
实际模型及降级原因。启停需要受限的 WSL 控制服务，不会通过网页执行任意命令。
配置步骤见 [本地千问运维](docs/local-qwen.md)。

完整字段和可选服务见 [`.env.example`](.env.example)。

集群只读接入默认关闭。它使用独立控制进程和两份用途不同的凭据，部署、验收和
回滚步骤见 [集群控制运行手册](docs/cluster-control-runbook.md)。

### 4. 启动 Bot

```bash
python bot.py
```

默认监听 `127.0.0.1:8080`。根路径没有网页内容，返回 `Not Found` 是正常现象。

### 5. 连接 NapCatQQ

在 NapCatQQ 中新增 OneBot V11 反向 WebSocket：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

NapCat 日志出现连接成功后，可以在 QQ 中测试：

```text
/ai 你好
@机器人 帮我解释一下递归
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `/ai 问题` | 发起普通 AI 对话 |
| `/搜 关键词` | 直接联网搜索，不经过模型整理 |
| `/模型` | 查看当前可用模型与选择 |
| `/模型 profile` | 为当前用户切换模型 |
| `/模型 默认` | 恢复群默认模型 |
| `/effort [档位]` | 查看或设置当前会话的推理强度 |
| `/shell 命令` | 在当前群共享的 Docker 工作区执行命令，不经过 LLM |
| `/shell status` | 查看当前群命令行沙盒 |
| `/shell reset` | 销毁当前群命令行沙盒并清空工作区 |
| `/识图` | 读取当前、引用或最近图片 |
| `/听` | 转写引用或最近 QQ 语音 |
| `/语音 内容` | 使用语音回答 |
| `/表情` | 从全局表情库发送图片表情 |
| `/qq表情 名称` | 发送 QQ 自带表情 |
| `/表情状态` | 查看表情库状态 |
| `/记忆` | 查看或管理长期记忆 |
| `/task 任务内容` | 让主控拆分任务并派给固定专业 Sub-Agent |
| `/任务` | 查看当前普通任务和 Sub-Agent 任务 |
| `/停止 task#编号` | 取消指定 Sub-Agent 任务 |
| `/usage` | 查看当前会话用量 |
| `/pin`、`/pins` | 固定消息或查看固定列表 |
| `/ai_reset` | 重置当前会话的 AI 上下文边界 |
| `/clear` | 清理当前会话的上下文和存储数据 |

`/shell` 会自动创建当前群专属的高级沙盒，同一个群复用 `/workspace`，不同群之间隔离。
它不会执行 h610 宿主机命令，也不会消耗模型 Token。普通命令最长运行 30 秒：

```text
/shell pwd
/shell ls -lah
/shell python --version
/shell echo hello > hello.txt
```

Agent 使用同一套精简 Nix 沙盒。Python、Node、GCC、Git、SQLite、常用 shell
命令和中文 PDF 工具开箱即用；Go、Rust、Java、ffmpeg、LibreOffice、OCR 与
科学计算栈由 `nix_search` + `sandbox_exec.packages` 按任务加载并共享缓存，避免
把低频工具永久塞进二十多 GB 的基础镜像。任务执行容器以非 root 用户运行，
根文件系统和 Nix 缓存只读；任务结束后工作卷自动删除，按需包缓存定期回收。

OpenAI/CLIProxy 模型可以按当前用户、当前会话覆盖推理强度：

```text
/effort
/effort high
/effort xhigh
/effort default
```

自然语言请求不依赖关键词硬编码。`@机器人 看看这张图`、`帮我查一下最新消息`、
`创建一个 Python 项目并把文件发出来` 等请求会进入同一个 Agent Loop，由当前模型决定
是否调用相应工具。

## Sub-Agent 任务模式

Sub-Agent V2 内核区分直接回答、单专家委派、后台工作流和已有任务修订。普通问答由主 Agent 直接完成；边界明确的单一专业子任务由
主模型通过 `delegate_agent` 交给独立专家，专家返回结构化证据后仍由 Kenneth 统一回复；明显
包含多个专业领域、互相依赖步骤或长时间工作的请求才使用 `run_subagents`。宿主路由器可以在
主 ReAct Loop 前自动升级复杂任务，`/task` 只是强制进入工作流模式的手动入口。主控先生成受宿主校验的
无环任务图，再把步骤派给七个固定角色：主控、搜索、代码、文件、媒体、分析和运维。每个
角色开始和完成时都会通过 `say` 汇报正在做什么。执行角色不能自行创建新 Agent，最多步骤、
并行数、工具轮次和总超时均由宿主配置。

每个专家拥有声明式、带版本的模型策略、工具白名单、风险等级、重试次数、轮次和超时配置。
宿主先构造任务级 `ContextPacket`，再按角色允许的通道投影成不可变的 `AgentContext`：搜索角色
只看到会话、来源和上游证据，代码与文件角色才看到沙盒产物，个人记忆默认不下发给执行角色。
每个 Run 的上下文哈希和快照单独写入 PostgreSQL；重试和重启续跑复用原快照，不会吸入后来
出现的群消息。Agent 以统一结构返回摘要、事实、证据、产物、限制、未解决事项和置信度。

```text
/task 分析刚才的视频，核实里面提到的产品参数，再生成一份 PDF 报告
/task 读取群文件里的项目，修复测试并把修改后的文件发回来
```

每个角色只能看到与职责匹配的工具。例如搜索 Agent 不能执行 Shell，代码 Agent 只能在
隔离沙盒中运行命令，运维 Agent 默认只能查看权威告警和任务状态。步骤、依赖、模型、工具、
上下文快照、工具风险与副作用、结果和错误都写入 PostgreSQL，并在管理台“任务与投递”页面中
实时展示。网络类瞬时故障最多按角色策略安全重试；一旦出现不可重复副作用就停止自动重试。
新工作流进入 PostgreSQL 持久队列，独立 worker 用租约、心跳和写入校验接管；聊天请求结束或
机器人重启不会丢掉已接受的任务。未知的外部副作用不会盲目重做。旧版本任务保留手动断点恢复。
已完成步骤不会重跑；业务失败或验收不通过时最多追加两个受限修复节点，不会无限重画计划。

每个步骤使用独立 Docker 容器。上游交付物按 SHA-256 存成不可变快照，通过 `import_agent_artifact`
按依赖授权交接；不是靠提示词假装隔离。原始快照不允许子 Agent 修改，构建时复制到自己的目录。
文件先做宿主格式检查，再由独立 Agent 检查任务目标；PDF 检查结构、字体嵌入并渲染首页，
不把这些机器检查夸大成所有页面的人工视觉验收。文件是否可读、内容是否验收通过、QQ 是否收到
分别记录，不能用其中一项代替另外两项：

- **验收通过**：按正常产物交付，实际群文件回执确认后才标记送达。
- **可读但尚未完成验收**：通过宿主可读性和快照校验的产物，可以作为“未完成草稿”交付，
  明确说明未核实的内容或未通过项；草稿送达不会让任务变成“全部完成”。
- **文件损坏或缺少必要校验**：阻止交付并说明原因，不把不可用文件冒充草稿。

结果不明确的上传会核对群文件里的唯一名称、大小和上传者，不盲目重复上传。
临时容器结束后清理，文件快照保留用于修订。

在同一群由原发起人说“把刚才那个项目的前端改一下”即可追加修订；保留原 task 编号和各步骤
自己的会话，仅重做选中步骤及其下游。控制台“任务与投递”可分别设置任务默认和各角色的模型：
`auto` 自动匹配职责，`preferred` 首选并允许降级，`locked` 锁定且禁止静默降级。
默认代码/分析/主控偏好 Terra，搜索/文档偏好 Luna，保留 Qwen 等可用备选；自动链不使用 Sol，
只有明确指定才启用。页面分别展示执行、独立验收和文件投递状态，模型记录可悬停查看 Token。

部署与边界说明见 [Sub-Agent V2 运维说明](docs/subagent-v2-operations.md)。
重启接管、外部作业等待和最终回复投递见 [任务持久化与恢复](docs/task-persistence.md)。

## 上下文与记忆

每条 QQ 消息会写入不可变的规范消息账本。模型看到的是 `msg#12`、`[mention#3]`、
`image#12.0`、`t#8` 等内部句柄；原始 QQ 号、群号和 NapCat 消息 ID 留在本地适配层。

上下文由四部分组成：

1. 当前问题、引用链和被 @ 的完整句子。
2. 当前群最近的原始消息窗口。
3. 较旧消息生成的分段摘要和按需展开证据。
4. 当前群、当前用户可见的 Pins 与长期记忆。

消息事实按群聊或私聊隔离，个人记忆进一步按用户隔离。模型不能通过猜测内部编号读取
其他群的数据。`/clear` 只影响当前会话，不会清除其他群的上下文。

每个 Agent 请求还会生成持久回合 `t#`，记录模型、工具调用、进度、最终回答和异常状态。
引用机器人之前的任务回复继续提问时，新回合可以继承旧回合摘要和期间新增的群聊消息。

## 图片与表情

普通图片和表情使用两条独立链路：

- **普通图片**：创建一次性 `vision_jobs`，按图片地址调用视觉模型。交付结果后清理 URL 和
  识别结果，不保存图片 Blob。
- **QQ 表情**：只有平台明确标记为表情的图片才会下载，按 SHA-256 去重，并经过真人、隐私和
  安全检查后进入永久表情库。

所有群共享同一套安全表情库。`send_sticker` 会综合标签相关度、近期发送记录和历史使用次数
选择候选；用户说“换个”时会排除刚发过的图片，没有其他匹配候选就明确返回没有。

管理台中的群“识图”开关只控制自动图片介绍：开启后，成员单独发送不带文字的普通图片时，
机器人会自动回复简短简介；关闭后仍然可以通过 @ 或 `/识图` 手动查看图片。

## 帖子与视频

`inspect_shared_content` 可以读取群里的 `source#`、`msg#` 或完整链接。普通模式读取页面元数据、
正文和评论；B 站链接以及 QQ 当前消息、引用消息或同一用户最近五分钟内发送的原生视频，
都支持额外的深度模式：

1. 获取 B 站低清 DASH 流，或带签名的 QQ 原生视频媒体流。
2. 临时下载媒体文件。
3. 使用 FFmpeg 均匀抽取关键帧。
4. 使用本地 whisper.cpp 转写音轨。
5. 把关键帧、转写、标题和用户问题交给视觉模型综合分析。
6. 删除临时媒体文件；B 站结果可短期缓存，QQ 原生视频不会长期保存。

默认深度分析上限为 60 分钟、1GB 和 12 张关键帧。它不是逐帧审片，转写也可能误识别
专有名词，回答中会保留这些限制说明。

## Docker 沙箱

启用沙箱后，Agent 可以在临时 Docker 容器中：

- 创建和修改项目文件。
- 安装依赖、执行命令和运行测试。
- 导入当前群上传的文件。
- 把构建产物、压缩包或图片发回当前群。
- 使用 `say` 汇报长任务进度。

沙箱不挂载宿主机目录，并按“群 + 发起用户”授权。任务结束后，工作容器会停止但不会删除，
`/workspace` 和独立产物快照仍可用于复查、修订和补发；模型不能调用销毁工具。真正删除由
管理员在控制台“沙盒”页面明确执行，删除前需要二次确认；运行命令期间禁止停止或删除。
生产环境仍应限制并发并定期审核保留项，否则磁盘占用会持续增长。

```dotenv
AI_SANDBOX_ENABLED=true
AI_SANDBOX_ALLOWED_USERS=123456789
AI_SANDBOX_MAX_PER_USER=2
AI_SANDBOX_MAX_TOTAL=8
AI_SANDBOX_TIMEOUT_SECONDS=120
```

## 管理控制台

启用管理台：

```dotenv
AI_ADMIN_ENABLED=true
AI_ADMIN_SECRET_FILE=/run/secrets/gaoji/admin-authorization-key
AI_ADMIN_ORIGIN=https://bot.example.com
AI_ADMIN_BOT_ID=机器人QQ号
AI_ADMIN_PATH=/bot-admin
```

访问：

```text
http://127.0.0.1:8080/bot-admin
```

先按 [账户与手机授权部署说明](docs/account-security.md) 迁移数据库并创建首个管理员。
账户密码登录后，普通成员仅能查看机器人基础状态；下面的详细信息与管理功能仅对管理员开放。
重要服务器命令会私聊绑定的管理员 QQ，直接回复收到的 6 位验证码即授权，当前任务和子任务自动继续，无需再点确认。

管理员控制台提供：

- Bot、NapCat、后台 Worker 与数据库状态。
- 模型 profile 的协议、模型名和能力开关。
- “我自己”和“其他群友”两类默认模型配置。
- 每个群的启用开关、自动识图开关和成员单独覆盖。
- 当前沙箱、执行任务、媒体审核、识图队列和分享内容状态。
- Token 用量、Agent 回合、工具调用、Trace 和投递状态。
- 上下文调试：查看当前话题、关联原消息、候选分数与淘汰原因、Token 分区、群/个人记忆和 Historian 队列；可标记“答对了”或“答非所问”。
- 工具权限开关；停用后下一轮模型请求不会再看到对应 Tool Call。
- PostgreSQL 持久审计、资源版本和乐观并发冲突保护。

页面由 React + TypeScript 构建，通过 `/api/v1` 管理 API 读取数据，并用 SSE 只更新发生变化的
资源。正在操作的下拉框会保留本地草稿和焦点，不会因为实时数据更新而关闭。所有控制台写
操作都会携带当前资源版本；多人同时修改时，旧版本提交返回 `409` 并重新加载最新状态。

推荐只通过 Tailscale、反向代理或 SSH 隧道在可信内网访问。不要把无 Token 的管理台直接暴露
到公网。

## Nix 与 NixOS

仓库可直接构建和运行：

```bash
nix build
nix run
nix flake check
```

在自己的 NixOS flake 中引用：

```nix
inputs.qq-bot = {
  url = "github:zty20040403/gaojibot";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

把模块加入目标主机：

```nix
imports = [inputs.qq-bot.nixosModules.gaoji];

services.gaoji = {
  enable = true;
  environmentFile = "/run/secrets/gaoji.env";
  host = "127.0.0.1";
  port = 18080;

  sandbox.enable = true;
  browser.enable = true;
  codesnap.enable = true;
  videoDeep.enable = true;
};
```

`environmentFile` 应位于 Nix store 外部并由 sops-nix、agenix 或其他密钥系统生成。
不要把 API Key、管理 Token 或数据库密码直接写进 Nix 表达式。

更新服务器前建议严格按下面的顺序操作，避免共享配置被旧工作树回滚：

```bash
git fetch origin
git status -sb
git pull --ff-only
sudo nixos-rebuild switch --flake .#your-host
```

## 数据库与归档

PostgreSQL 是消息、上下文、模型选择、记忆、工具日志、媒体元数据和任务状态的事实来源。
Alembic 在升级时管理 schema 版本，NixOS module 默认在服务启动前执行迁移。

生产环境可以把低延迟节点作为首选数据库，把容量更大的节点用于复制、备份和冷归档。
普通回复不应同步读取冷归档；旧媒体、投递正文和大文件由后台任务异步迁移，归档不可用时不会
阻塞普通聊天。

旧 SQLite/JSON 数据的迁移方法见
[`docs/postgresql-migration.md`](docs/postgresql-migration.md)。完整生产配置和故障边界见
[`docs/operations-v3.md`](docs/operations-v3.md)。

## 开发与测试

进入固定开发环境：

```bash
nix develop
```

运行测试：

```bash
AI_ALLOW_LEGACY_SQLITE=true python -m unittest discover -s tests
nix flake check
```

提交前至少检查：

```bash
git diff --check
git status -sb
```

涉及数据库、工具权限、消息 Scope、媒体发送或 NixOS module 的改动，应补充相应的定向测试。

## 安全边界

- `.env`、API Key、数据库 DSN、QQ Token 和管理 Token 不应提交到 Git。
- 模型只获得当前会话可见的规范句柄，工具执行时会再次检查 Scope 与权限。
- 图片 OCR、网页内容、群文件和工具返回值都视为不可信输入，不能覆盖 system prompt。
- Docker 沙箱不是宿主机管理员权限；不要挂载宿主目录或 Docker Socket 到任务容器。
- NapCat 和 QQ 账号应运行在受控环境中，并定期备份数据库与表情 Blob。
- 管理控制台应使用强 Token，并限制在可信网络访问。

## 文档

- [从零搭建教程](docs/from-zero.html)
- [生产运维与配置](docs/operations-v3.md)
- [PostgreSQL 迁移](docs/postgresql-migration.md)
- [系统架构](docs/architecture.md)
- [架构决策记录 ADR](docs/adr/README.md)
- [0.18 命名与升级说明](docs/rebranding.md)
- [第三方组件声明](THIRD_PARTY_NOTICES.md)

## 许可证

MIT License · Copyright (c) 2026 Kenneth。详见 [LICENSE](LICENSE)。
