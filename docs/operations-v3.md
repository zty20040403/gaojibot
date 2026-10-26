# 0.3.0 可选基础设施启用手册

基础 QQ 对话只需要 NapCat、NoneBot 和一个模型 profile。下面功能都能独立开启，建议一次
只开一组，启动后先看日志和管理页，再继续下一组。

## 1. 安装新增依赖

```bash
cd /path/to/gaojibot
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

`psycopg` 用于 pgvector，`playwright` 用于持久浏览器和代码/表格截图，`Pygments`
用于代码高亮。Chromium 会额外占用磁盘；不安装时机器人仍可对话，代码和表格只会
按普通文本发送。

## 2. 多模型底座

不填写新配置时，程序会把原来的 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、
`DEEPSEEK_MODEL` 和 `DEEPSEEK_THINKING` 合成为一个名为 `deepseek` 的兼容 profile，
并保留 `flash`、`pro` 两个旧切换入口，原有部署不需要立刻迁移。

显式多模型配置使用一行 JSON；API Key 只引用环境变量名，不要直接写进 JSON：

```text
DEEPSEEK_API_KEY=你的DeepSeekKey
OPENAI_API_KEY=你的OpenAIKey
ANTHROPIC_API_KEY=你的AnthropicKey
AI_MODEL_DEFAULT_PROFILE=deepseek
AI_MODEL_PROFILES_JSON={"default":"deepseek","profiles":{"deepseek":{"provider":"deepseek","protocol":"openai-chat","base_url":"https://api.deepseek.com","api_key_env":"DEEPSEEK_API_KEY","model":"deepseek-v4-flash","thinking":"disabled","aliases":["ds","flash"]},"openai":{"provider":"openai","protocol":"openai-chat","base_url":"https://api.openai.com/v1","api_key_env":"OPENAI_API_KEY","model":"gpt-5-mini","aliases":["gpt"]},"claude":{"provider":"anthropic","protocol":"anthropic-messages","base_url":"https://api.anthropic.com","api_key_env":"ANTHROPIC_API_KEY","model":"claude-sonnet-4-6","max_output_tokens":4096}}}
AI_GROUP_MODEL_PROFILES_JSON={"<GROUP_ID>":"openai"}
```

每个 profile 支持这些主要字段：

| 字段 | 作用 |
| --- | --- |
| `provider` | 日志和用量统计中的供应商名称 |
| `protocol` | `openai-chat` 或 `anthropic-messages` |
| `base_url` | 此 profile 自己的 API 地址 |
| `api_key_env` | 保存密钥的环境变量名 |
| `model` | 发给供应商的真实模型 ID |
| `aliases` | `/模型` 可接受的短名称 |
| `timeout_seconds` | 单次 HTTP 请求超时，范围 1 到 600 秒 |
| `temperature` | 可选采样温度；留空则由供应商默认 |
| `thinking` | `auto`、`enabled` 或 `disabled` |
| `reasoning_effort` | 可选默认推理强度：`minimal`、`low`、`medium`、`high`、`xhigh`、`max` 或 `none` |
| `max_output_tokens` | 输出 token 上限；Anthropic 默认 4096 |
| `api_key_required` | 本地免鉴权兼容服务可显式设为 `false` |
| `capabilities` | 可覆盖 `tools`、`streaming`、`json_mode`、`model_listing`、`vision` |

当前 `openai-chat` 支持工具、流式输出和原生 JSON mode；它可连接实现 Chat
Completions 协议的兼容 endpoint。`anthropic-messages` 已支持普通对话和客户端工具调用，
但当前适配器使用完整响应而不是流式事件；JSON 后台任务通过严格提示和宿主解析完成。
profile 若声明不支持工具，机器人仍能普通回答，但不会给该模型执行工具。
`vision` 表示该 profile 可以接收图片。启用视觉服务后，普通图片只会把短期 QQ 地址
放进 `vision_jobs`，由 `AI_VISION_PROFILE` 指定的视觉模型即时生成结果；交付后会清除
地址和结果，不保存图片原件。只有 QQ 明确标记的表情才按 SHA-256 去重保存在宿主机。
普通聊天 profile 即使不支持视觉，也能通过 `view_image` 创建一次性识图任务；原图只会
发送给配置的视觉模型服务。

重启后在群里使用：

```text
/模型
/模型 claude
/模型 默认
/effort
/effort high
/effort default
```

选择按用户和会话隔离，只持久化 profile 名。群级默认可由
`AI_GROUP_MODEL_PROFILES_JSON` 声明，优先级为用户选择、群级默认、全局默认。回合重放还会同时核对 provider、profile、
真实 model、提示词版本和工具目录；切换供应商后旧回合自动退化成摘要，不会把两种协议
的原样 tool segment 混在一起。配置变更需要重启。当前不会在 Agent 工具回合中自动
故障转移，因为重跑可能重复发送文件、写记忆或执行其他有副作用操作。

### 2.1 临时识图与持久表情库

```text
AI_MEDIA_ENABLED=true
AI_MEDIA_ROOT=/var/lib/gaoji/media
AI_VISION_PROFILE=gpt-5.6-luna
AI_VISION_AUTO_DESCRIBE=false
AI_MEDIA_MAX_SOURCE_MB=100
AI_VISION_MAX_IMAGE_MB=20
AI_MEDIA_PREPARE_THRESHOLD_MB=1
AI_MEDIA_MAX_EDGE_PX=1568
AI_VISION_TIMEOUT_SECONDS=180
AI_MEDIA_MAX_ATTEMPTS=5
AI_MEDIA_LEASE_SECONDS=600
AI_MEDIA_BATCH_SIZE=4
AI_MEDIA_WORKER_CONCURRENCY=2
```

服务需要 PostgreSQL，并建议把 `ffmpeg` 放进服务的 `PATH`。普通图片通过
`view_image(mode=summary)` 生成简短介绍；用户要求仔细看时，
`view_image(mode=detail)` 会根据当前、回复或最近图片的地址重新调用 Luna。GIF 和
过大的图片只在临时目录提取、压缩首帧，识别结束后删除。普通图片没有 Blob、分析表或
历史检索入口。
`AI_VISION_AUTO_DESCRIBE=true` 时，单独发送的普通图片会自动得到十几字简介；带文字或
@机器人的图片不会触发这条旁路，而是继续由 Agent Tool Call 处理。

QQ 表情使用独立持久任务队列，模型可调用 `find_stickers` 和 `send_sticker`。安全表情
在所有群之间共用同一库存与全局向量索引；指定标签没有匹配时返回“没有这个表情”。

自动表情库采取 fail-closed 策略：包含真人、聊天记录、二维码、联系方式或其他隐私
信息的图片不会成为可发送表情；`review` 和 `blocked` 状态也不能被 `send_sticker`
绕过。管理页的“视觉与表情”分别显示永久表情 Blob 和不含图片内容的临时任务状态。
升级前由旧版流水线保存的普通图片不会在迁移中自动删除，也不会再被工具或管理页读取。
生产清理由独立维护任务先统计和预览，再经管理员确认执行。

## 3. 持久 outbox 与流式发送

```text
AI_OUTBOX_ENABLED=true
AI_STREAM_ENABLED=true
```

outbox 状态保存在 PostgreSQL 的 `deliveries` 与 `delivery_attempts` 表：

- `pending`：等待投递。
- `sending`：已领取租约，正在请求平台。
- `committed`：平台返回确定回执，或后续 self-message echo 对账成功。
- `ambiguous`：请求超时、进程中断或租约过期，平台可能已经发出；不会盲目重试。
- `failed`：平台明确拒绝，或安全重试次数用完。
- `cancelled`：管理员取消。

Matrix 使用稳定 transaction id，网络失败可安全重试；OneBot 和 iMessage 超时会先
停放。确认目标会话确实没收到后，才能在管理页手动重试 `ambiguous` 项。

流式输出只发送已经闭合的完整段落。未闭合代码块、`[silence]` 和还没有段落边界的
短回答会等模型完成；`/停止` 会取消任务并关闭当前 provider 的 HTTP stream。

## 4. 管理页与配额

```text
AI_ADMIN_ENABLED=true
AI_ADMIN_SECRET_FILE=/run/secrets/gaoji/admin-authorization-key
AI_ADMIN_ORIGIN=https://bot.example.com
AI_ADMIN_BOT_ID=机器人QQ号
AI_ADMIN_PATH=/bot-admin
AI_ADMIN_USER_IDS=<ADMIN_QQ>
AI_QUOTA_ENABLED=true
AI_QUOTA_DAILY_CALLS=100
AI_QUOTA_DAILY_INPUT_TOKENS=500000
AI_QUOTA_DAILY_OUTPUT_TOKENS=100000
```

启动后访问 `http://127.0.0.1:8080/bot-admin`。页面可查看 outbox、token 用量、运行
任务、桥接和浏览器状态，也可调整群默认、管理员本人和其他群友的模型。网页模型
选择写入 PostgreSQL 后立即生效，不需要 rebuild。页面还可停止任务、重试或取消投递。
若把 `HOST` 改为公网地址，
必须初始化账户登录与 QQ 私聊授权，并在反向代理上配置 TLS。
普通成员仅能看基础状态；管理员修改需本人私聊回复 6 位口令。首次迁移与初始化见 [账户与手机授权](account-security.md)。

`/bot-admin/api/overview` 还会列出当前存活的后台 worker、模型 profile 的非敏感配置
以及最近一次未捕获异常。
worker 意外退出不会被误报成“功能仍正常”；生产监控应对非空 `failures` 告警。

数值 `0` 表示不限制。当前配额按 canonical ConversationScope 和上海自然日计算，
不会因为同一群的不同镜像端重复计费。

## 5. PostgreSQL + pgvector 语义召回

先准备装有 pgvector 扩展的 PostgreSQL。用 Docker/OrbStack 测试可运行：

```bash
docker run -d --name qqbot-pgvector \
  -e POSTGRES_DB=qqbot \
  -e POSTGRES_USER=qqbot \
  -e POSTGRES_PASSWORD=请换成强密码 \
  -p 127.0.0.1:5432:5432 \
  -v qqbot-pgvector:/var/lib/postgresql/data \
  pgvector/pgvector:pg17
```

再配置一个确实提供 `/v1/embeddings` 的 OpenAI-compatible 服务：

```text
AI_SEMANTIC_ENABLED=true
AI_POSTGRES_DSN=postgresql://qq_bot:replace-password@db-primary.example:5432/qq_bot
AI_EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
AI_EMBEDDING_API_KEY=ollama-local
AI_EMBEDDING_MODEL=bge-m3
AI_EMBEDDING_DIMENSIONS=1024
AI_SEMANTIC_INDEX_SECONDS=60
AI_SEMANTIC_BATCH_SIZE=32
```

DeepSeek Chat API 本身不等于 embedding API，不能直接把聊天模型名填在这里。示例使用
本机 Ollama 的 OpenAI-compatible embedding 接口；`ollama-local` 只是启用客户端所需的
非敏感占位值。维度必须
与 embedding 服务实际返回一致。向量表带 Scope、来源句柄和 HNSW cosine 索引；
原始消息以 PostgreSQL `messages` 账本为准，删掉向量表后可以重新生成。

## 6. Historian 与 Dream

```text
AI_HISTORIAN_ENABLED=true
AI_HISTORIAN_PROFILE=
AI_HISTORIAN_MODEL=
AI_HISTORIAN_CHECK_SECONDS=60
AI_HISTORIAN_IDLE_SECONDS=600
AI_HISTORIAN_MAX_ATTEMPTS=4
AI_DREAM_ENABLED=true
AI_DREAM_PROFILE=
AI_DREAM_MODEL=
AI_DREAM_HOUR=4
AI_DREAM_MIN_ENTRIES=15
```

profile 留空时使用默认 profile；`AI_HISTORIAN_MODEL` 和 `AI_DREAM_MODEL` 只用于在所选
profile 内兼容覆盖真实模型 ID。群聊安静 10 分钟后，Historian 把连续且哈希确定的
消息范围写入持久任务队列。任务带租约，进程重启后会继续；模型连续失败到最后一次
才发布可追溯的规则摘要兜底。P1/P2/P3、P4 检索锚点、话题、重要性、置信度、参与者
和证据消息均随章节保存。Historian 独占章节游标，聊天请求不会抢先机械压缩；发布时
cursor 已变化则 CAS 失败，不会覆盖新状态。Dream 每天在指定小时做一次长期记忆整理，更新必须携带当前版本，
因此群聊过程中刚被人修改的记录不会被后台静默覆盖。

两者都会额外消耗模型额度，建议先只开 Historian，观察管理页用量后再开 Dream。

### 6.1 Recall Router、Reranker 与证据检查

```text
AI_RECALL_ROUTER_MODEL_ENABLED=true
AI_RECALL_ROUTER_PROFILE=deepseek
AI_RECALL_ROUTER_TIMEOUT_SECONDS=8
AI_EVIDENCE_GUARD_ENABLED=true
```

每轮先由规则选择 `direct`、`follow_up`、`recent_group`、`old_topic`、
`user_memory`、`group_memory` 或 `no_recall`。高置信度规则不会调用模型；只有短而
模糊的消息才使用路由 profile 做一次小型 JSON 分类。`no_recall` 不读取群历史或
长期记忆，减少无关 Token 和话题串味。

候选统一经过 Scope 硬过滤和混合 Reranker。相对最高分达到阈值的互补证据可以一起
保留，不会永远只选一条；重复、话题冲突和用户归属风险会扣分。上下文预算再按问题
复杂度、模型 `max_input_tokens` 和 `AI_CONTEXT_INPUT_BUDGET_TOKENS` 动态分给焦点、
时间线、旧消息、群记忆、个人记忆与工具结果。

Evidence Guard 在主模型生成前检查话题、证据、用户归属和明显时间冲突。模糊追问
没有可靠焦点时直接请求引用，而不是让模型带着错误上下文自信回答。每轮路由、预算、
候选分数和检查结果都写入 `turn_context_plans`，供 Trace 和管理端审计。
管理台“上下文调试”页会按回答还原上述决策，同时展示最终证据原消息、各分区实际
Token、群记忆与个人记忆来源，以及 Historian 的积压、重试和失败任务。管理员反馈
通过 `turn_context_feedback` 持久化，并使用控制面资源版本和审计日志防止并发覆盖。

## 7. 持久浏览器与富消息截图

```text
AI_RICH_RENDER_ENABLED=true
AI_BROWSER_ENABLED=true
AI_BROWSER_MAX_SESSIONS=3
AI_BROWSER_IDLE_SECONDS=1800
AI_BROWSER_ALLOW_PRIVATE_NETWORK=false
```

模型可调用 `browser_navigate`、`browser_snapshot`、`browser_click`、`browser_type`、
`browser_press_key`、`browser_wait_for`、`browser_scroll`、`browser_close`、
`browser_clear`。页面快照只给
模型 `b1`、`b2` 这类宿主生成的元素引用，不接受任意 CSS selector。profile 位于
`AI_STATE_DIR/browser_profiles`，按发起者哈希隔离并在重启后保留登录状态。
`/clear` 或 `browser_clear` 只删除当前发起者自己的 profile。

默认拒绝 localhost、`.local`、IP 私网和解析到非公网 IP 的地址。浏览器仍是宿主
进程，不应用它打开不可信内网页面，也不要在共享群里登录重要账号。

## 8. Bilibili 与合并转发

这两项无需额外开关。模型在群聊工具回合中可调用：

```text
view_bilibili
view_forward
```

`view_bilibili` 读取公开视频元数据和有限条热门评论；`view_forward` 只允许展开当前
Scope 已有的 `msg#`，子消息中的原生 QQ 号和嵌套 forward id 不会交给模型。

## 9. Matrix 镜像

示例 bundle：

```text
AI_MIRROR_ROUTES_JSON=[{"name":"main","endpoints":[{"platform":"onebot-v11","kind":"group","id":"QQ群号","bot_user_id":"机器人QQ"},{"platform":"matrix","kind":"group","id":"!room:example.org","bot_user_id":"@bot:example.org"}]}]
AI_MATRIX_ENABLED=true
AI_MATRIX_HOMESERVER=https://matrix.example.org
AI_MATRIX_ACCESS_TOKEN=Matrix机器人access token
AI_MATRIX_USER_ID=@bot:example.org
AI_MATRIX_APPSERVICE_TOKEN=单独生成的桥接token
```

程序使用 `/sync` 持久 cursor，timeline 有 gap 时用 `/messages` 回填；发送用稳定 txn id。
若同时配置 Application Service，让 homeserver 把交易发到：

```text
PUT /_matrix/app/v1/transactions/{txn_id}?access_token=AI_MATRIX_APPSERVICE_TOKEN
```

一个 endpoint 只能属于一个 bundle。bundle 中存在 OneBot 时它固定为 canonical；
同一事件在 `/sync` 与 Application Service 重复到达也会按原生 event id 去重。

## 10. BlueBubbles iMessage 镜像

```text
AI_IMESSAGE_ENABLED=true
AI_IMESSAGE_BASE_URL=http://127.0.0.1:1234
AI_IMESSAGE_PASSWORD=BlueBubbles服务密码
AI_IMESSAGE_WEBHOOK_TOKEN=单独生成的随机token
AI_IMESSAGE_CHAT_GUID=iMessage chat GUID
AI_IMESSAGE_BOT_HANDLE=机器人发送账号
```

把 BlueBubbles webhook 指向：

```text
POST http://机器人地址:8080/bot-bridge/bluebubbles?token=AI_IMESSAGE_WEBHOOK_TOKEN
```

并在 `AI_MIRROR_ROUTES_JSON` 中加入 `imessage` endpoint。iMessage POST 没有 Matrix 那样
的幂等 transaction id，超时会进入 `ambiguous`，等 webhook echo 或管理员检查。

桥接 token 为空时 webhook 返回 503，这是故意的 fail-closed 行为。

## 11. 验证

不启动真实机器人也能运行：

```bash
python -m compileall -q src tests
AI_ALLOW_LEGACY_SQLITE=true python -m unittest discover -s tests -v
```

单元测试显式打开 legacy 模式，是因为它们会创建隔离的内存 SQLite 数据库；这不代表
生产会回退 SQLite。真实 PostgreSQL 集成测试另设 `TEST_POSTGRES_DSN`，详见
`tests/test_postgres_integration.py`。

然后再启动 `python bot.py`，依次观察：OneBot 连接、outbox worker、可选 Matrix sync、
semantic worker、Historian/Dream 日志。不要一次同时修改所有开关，否则某个外部服务
配置错误时很难判断是哪一层。
