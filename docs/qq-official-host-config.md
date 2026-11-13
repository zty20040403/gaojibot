# 官方 QQ 宿主配置

`AI_QQ_OFFICIAL_HOST_CONFIG_FILE` 指向服务用户可读的私有 JSON 文件。NixOS 用 `services.gaoji.qqOfficial.hostConfigFile` 加载为 systemd credential。不要放密钥值、Python 导入路径或未经核实的身份映射。

下面使用测试身份展示字段。正式 `account` 必须由真实 AppID 与环境组成，文件目录须位于服务允许写入的私有状态目录。

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP_ID\",\"production\"]",
  "files": {
    "private_directory": "/var/lib/gaoji/state/qq-official-files",
    "generation_enabled": true,
    "max_file_bytes": 104857600
  },
  "media": {
    "ocr": true,
    "asr": true,
    "local_asr": false,
    "documents": true,
    "forwards": true,
    "stickers": true
  },
  "native_background": {
    "proactive": {
      "enabled": false
    }
  }
}
```

文件通过当前官方账号上传，不再配置 `native_routes` 或上传证明。实际大小还受 VM 文件导出上限和平台返回限制约束；宿主和 VM 的上限应保持一致。`generation_enabled` 需要工作区后端可用。

图片说明入库另由管理台“媒体审核 → 图片说明”按会话启用，默认关闭、每日 200 张（北京时间），可调为 0–10,000 张。宿主须启用 `AI_MEDIA_ENABLED=true` 并配置支持视觉的 `AI_VISION_PROFILE`。启用后仅处理新入站图片/表情，不补旧图；说明可在 `history` 搜到。失败、超额和费用可查；关闭不退款已经发生的模型调用。普通图片不会自动加入全局可发表情库。

媒体能力按上面的开关启用。`ocr` 开关现在启用 `view_image` 和前台图片输入：前台 profile 的 `capabilities.vision=true` 时直接附当前/合并/引用图片（最多四张），否则用有效的视觉 profile 返回画面与文字。原图下载和模型图均受 `AI_VISION_MAX_IMAGE_MB`（默认 20 MiB，下载硬上限 100 MiB）限制；宿主须有 ffmpeg（Nix 包与开发 shell 已包含），缩到长边 1568，解码失败明确报错、不发送原始坏图。旧 OCR 描述回退仍沿用 `max_download_bytes` 上限和 `official_ocr` 用量分类；短语音优先读取腾讯识别文本，`local_asr` 需要宿主 whisper.cpp 与模型。视频和大附件先导入 VM，由后台处理。语音回复复用文件服务生成和发送 SILK。

视频直传可选：在宿主环境设置 `AI_VIDEO_PROFILE=<模型配置名>`（默认空），该 profile 必须为 `openai-chat`、显式 `capabilities.video=true`，且服务实际接受 Chat 的 `video_url` 数据 URL。建议 `max_input_tokens=65536`、`max_output_tokens=1024`、`timeout_seconds=180`；输入窗口至少覆盖每秒 100 token 加 1024 的余量。密钥只留宿主，不写入 VM。此约定不能等同于任意 OpenAI-compatible 服务都支持视频，需 owner 实测。

VM 需通过模板或显式工具升级安装 `gaoji-video`，将 `/work` 下视频转换为最长 10 分钟、1fps、长边 640、单声道、最大 32 MiB 的 MP4；超过时长先拒绝，不偷偷截断。导入结果的 `media_handle` 和路径一起交给后台。配置视频模型后后台有 `analyze_video`；未配置时用 ffmpeg 抽帧、`view_image`、`gaoji-transcribe`，并用 `record_video_summary` 保存结论。两者都写可搜索的会话历史说明；费用按原会话额度控制并关联后台任务。模型错误明确失败，不宣称已看完；未知费用保守保留。

主动接话启用时提供 `profile`，可指定 `max_reply_chars`。新消息去抖三秒，每个会话每小时最多八次；旧 `percent`、`threshold` 和 `max_checks_per_hour` 字段应删除。定时任务由 `schedules` 表和工具管理，不放在此文件。

## 宿主密钥 API（任务与 VM 令牌入口已实现）

可在宿主 JSON 的 `apis` 对象里登记声明；没有配置时保持关闭。中转服务提供 `/api/<名字>/<路径>`：VM 请求 `http://10.0.2.2:18640/api/<名字>/…`，头带 `Authorization: Bearer $GAOJI_JOB_TOKEN`（需展开环境变量）。非默认端口以后台提示词为准。任务结束/取消后任务令牌失效；没有令牌或失效任务令牌不会自动降级为 VM 身份。

VM 后端启动时中转服务随账号运行时启动，无需先有后台任务。会话 VM 的令牌在 `/run/gaoji/token`，连接地址在 `/run/gaoji/connection.json`；创建/启动时写入，宿主重启会核对仍在运行的 VM 的 boot 与令牌文件哈希再恢复，停止/销毁前撤销。程序调用 `/api` 可读取此文件设置 Bearer 头，不要把值复制进脚本、日志或 git。这个身份只能代表本会话，不能调用模型路由或 Codex TCP 通道，也没有创建 VM 的人的 owner 权限。

任务与 VM 调用共用按物理会话的 API 限额；审计分别标为 `job#…` 和 `vm:…`。`/stop` 取消任务身份，但按方案保留 VM 的 `/api` 能力，不停掉 VM；`/vm/say`、`/vm/wake` 与 `/vm/send-file` 均受停止门槛限制。此处接线不代表常驻单元管理已经完成。

```json
{
  "apis": {
    "test-api": {
      "base_url": "https://api.example.test/v1",
      "secret_file": "/run/secrets/TEST_API_KEY",
      "headers": {"Authorization": "Bearer {secret}", "User-Agent": "TEST_BOT"},
      "query": {},
      "allow": ["GET /repos", "POST /repos/TEST_BOT"],
      "per_hour": 20,
      "about": "测试 API；只按用户明确要求操作。"
    }
  }
}
```

- 名称只用小写字母、数字和连字符，最长 48 字符。`base_url` 仅 HTTPS，不含用户名、密码、查询或 fragment；请求只能留在这个固定基址下。
- `secret_file` 是宿主绝对路径，文件不得全局可读。`headers` 或 `query` 至少一处含 `{secret}`；密钥不进模型提示词或 VM。配置解析不读取密钥文件，调用阶段读取失败明确报错。
- `allow` 每项为 `METHOD /路径前缀`，按路径段边界匹配；`GET /repos` 允许 `/repos/TEST`，不允许 `/repos-private`。方法 `*` 表示支持的所有方法；不支持 glob 路径。省略/null 表示不限路径，空数组拒绝全部。CONNECT、TRACE 不开放。
- 拒绝点段、双斜杠、反斜杠、控制字符，以及编码的点/斜杠/百分号，不能靠重复解码绕过路径限制。小时额度为整数，0 表示不设本地小时上限；按物理会话持久计数，不因宿主重启清零；获准后失败或结果未知的调用也占次数。
- 请求体最多 64 MiB，响应头总等待最多 60 秒，连接/每块读取超时分别 10/60 秒；实际连接钉住公网 IP，保留原 Host/SNI/TLS 校验，不跟随重定向。剥离调用者的认证、cookie、方法覆盖和连接头；宿主配置的 header/query 覆盖同名字段。返回不含 Set-Cookie，拒绝不透明压缩及直接反射凭据的响应。
- 数据库审计只保存时间、物理会话、任务编号、API 名、方法、规范路径和结果，不存查询参数或请求体。管理员可在“任务与投递 → 宿主 API 调用审计”按 API 名/物理 scope 查询并翻页。`pending` 仅表示尚未确认结束，不证明外部未执行；取消和网络故障不自动重试写操作，也不退额度。流式响应中断将断开连接，不伪装成完整文件。
- API 能防密钥直接进入 VM，但不能替用户判断调用内容；群友可以让 VM 使用获准 API。需要限制收件人、目标仓库等内容时，不应配置一个无限制的通用入口。

### 程序直接发言（F2 部分实现）

仅 VM 令牌可请求 `POST /vm/say`，任务令牌不接受。JSON：

```json
{"request_id":"TEST_UNIQUE_REQUEST","program":"TEST_WATCHER","text":"TEST message"}
```

- `request_id` 必填，1–128 个字母/数字/`._:-`，首位须字母或数字；每个动作生成新 ID。重复 ID/相同内容不会重复发送或计额，变更内容则拒绝。
- `program` 可省略，默认 `manual`；名字最多 80 个 ASCII 字母/数字/`_.@-`，首位须字母或数字。它是审计标签，不是人的身份或权限。
- `text` 最多 1800 字符，整个请求最多 16 KiB。一次只发一条，不自动拆分来绕过额度。不可在正文声明 endpoint、actor 或 scope。
- 默认每个物理会话每整点小时 60 条；失败不退额度。停止、停用或来源撤销都拒绝，HTTP 前再次复查。只用主动消息；群未开放时明确失败，不借真人的被动回复窗口。
- 只有状态 `completed` 才代表已有平台回执。`202` 表示仍待完成，`409/ambiguous` 表示外部结果不明，`502/failed` 表示发送失败；不要换一个 ID 盲目重试。
- 用 `GET /vm/actions/<action_id>` 查状态。即使响应丢失、尚不知道 action_id，也可用 `GET /vm/actions?request_id=<原ID>` 查询；只读查询在 stop 期间仍可用，且不能查其他会话。

### 程序唤醒

`POST /vm/wake` 使用同样的 VM 认证和 JSON 字段；`text` 最多 4000 字符。返回 `202` 和动作 ID，之后用上述状态接口查询，不等待模型结束。

- 默认每物理会话每整点小时 6 次，超额合并到下一小时的一次唤醒；一个批次最多 64 条/64 KiB，满了明确返回 429。0 额度表示禁止。重试同 request_id 不重复唤醒。
- 程序没有真人或 owner 身份；模型可读本会话历史/记忆及只读工具，不能调用运维、后台任务或文件写工具。群 AI 必须已经开启。
- 模型答案也占每小时 60 条的发言额度；`completed` 且 `silent=true` 表示模型选择 `[silence]`，没有发送 QQ 消息；其他成功完成必须有真实发送回执。
- 停止、停用、凭据撤销及事件过期均会拒绝或取消。重启后未完成的模型回合标记 `ambiguous`，不重复调用；已保存答案可在来源仍有效时接续投递。

### 单次后台命令（F3）

`gaoji run-bg --request-id TEST_BUILD -- sh -lc 'cd /work/build && make'` 调用 VM-only `POST /vm/commands`。JSON 为 `{"request_id":"TEST_BUILD","program":"TEST_BUILD","argv":["make"]}`；program 可省略。请求最多 32 KiB，argv JSON 最多 16 KiB/128 项，不能带 scope、actor、VM 或任意宿主路径。任务令牌不能使用该入口。

- 返回 202 只表示持久登记；保存 command_id 和 request_id，不用 sleep 占住 agent。用 `gaoji command-status --command-id <id>` 或 `--request-id <原ID>` 查询，对应 `GET /vm/commands/<id>` 或 `/vm/commands?request_id=…`。退出结果与 notification 的送达状态分开。
- `gaoji stop-bg <id>` 对应 `POST /vm/commands/<id>/stop`，正文空对象。查询/取消在聊天停止期间仍允许；新命令在 guest/body 等待前捕获停止与 kill 双代数，旧启动和旧完成通知均被拒绝。
- 每物理会话最多八个未确认结束的命令；最长六小时、输出捕获最多 64 MiB，在 `/work` 以 sandbox 用户执行，不继承 agent 任务令牌。先准备 sandbox 可写目录；shell 语法须显式使用 sh -lc。命令和常驻程序是不同 systemd 单元。
- 控制器和监护只使用固定 guest helper。未知启动不重发，停止不明不释放名额；活跃/不明命令阻止空闲关机或清盘。宿主换租约后停止旧来源而不重执行，VM 重启也不重跑。新 boot 的程序读取旧命令状态不会恢复旧写权限。
- 完成通知走 F2 的受限模型唤醒和主动发送，额度/权限/平台失败都保留状态。VM 输出是资料，不授予真人/owner 权限。真实 QQ/VM/systemd 验收仍待 owner。

管理台“任务与投递”已展示命令、退出用时、输出快照和独立通知回执；默认只看未确认结束的命令，可切换最近记录并按物理 endpoint 筛选。最多100条，截断会提示。停止只登记持久意图，不在管理请求内执行 guest 命令；与账号锁、版本和审计同事务提交，要求登录、CSRF、If-Match，不走 QQ OTP。

同页可按会话设置新 agent 的时限1–6小时（默认6），也用于 routine 新建的 agent；创建后 deadline 固定，修改设置不延长旧任务。`ask_main` 独立最多等两小时，仍裁剪到任务剩余时间；活跃 agent 阻止会话 VM 两小时空闲清理。API 为 `GET /api/background-commands`、`PUT /api/background-commands/settings`（endpoint_key、hours）、`POST /api/background-commands/<id>/cancel`（endpoint_key）。程序来源没有修改这些管理设置的权限。

### 单元同步

`AI_PROGRAM_VM_LIMIT=4` 限制常驻 VM 名额（0–32；0 不加载登记单元），仍受 `AI_SANDBOX_MAX_TOTAL` 总 VM 上限约束。ready/运行中 VM 的宿主恢复会自动同步；未知同步结果保留名额，已登记但停用的程序保留磁盘。30 分钟两次非计划重启/启动失败后不再加载，VM 客户端不能解除暂停。宿主启动恢复离线常驻 VM，随后每 30 秒巡检、最多两路启动/恢复；人工停机不会被自动启动覆盖，容量不足不计崩溃。管理员可在“常驻程序”页检查日志后恢复自动加载；恢复任务不会解除停用或 `/stop`。`/units` 只读展示本会话持久单元状态和 24 小时已接收请求次数；前台下一轮上下文也包含停用、崩溃保护及最近投递错误，不把请求计数当成送达计数。

`POST /vm/units` 只接受空 JSON 对象，使用 VM 认证但不受聊天 stop 限制。`gaoji-units`/`gaoji units` 通过它调用宿主固定的 root guest helper，普通 sandbox 用户无须 sudo。宿主读取本会话 programs_enabled 与 program_controls；调用方不能提供策略、路径或命令。同步受单 VM 1 个/宿主 4 个并发和 guest 超时限制，失败返回非 2xx，不把已保存文件误当作已启动服务。

管理 API `/api/programs` 可按 endpoint 读取持久状态（包括宿主运行时离线时）；`/api/programs/settings` 设置会话开关与小时 say/wake 上限，`/api/programs/control` 按程序名停用/启用。修改要求登录、CSRF 和 If-Match，策略/版本/审计同事务，响应 `synchronization=pending` 表示正在等待 guest 同步。0 禁止该类动作，改额度或重新启用不退款。“常驻程序”页已接通；`/api/programs/resume` 将恢复意图与审计同事务入队，复查所看快照、实际路由及任务租约，一小时过期。页面显示恢复任务与逐单元结果，`/api/programs/messages` 按程序筛选实际账本消息，合并唤醒也计入对应成员。

### 程序发文件

`POST /vm/send-file` 接受 `{"path":"/work/report.txt","request_id":"TEST_UNIQUE","program":"TEST_PROGRAM"}`，可选 `filename` 改显示名。只从令牌绑定的会话 VM 工作区导出普通文件，不接受 URL、宿主路径、其他 VM 或 artifact ID。

返回 202 表示文件已不可变保存并排队，用动作状态接口查看结果；与 say 共用每小时额度，实际发送只走主动消息。同 request_id 不重复导出/发送，文件之后被改动也不会替换已排队的内容；未知结果不自动重新发送。

最多同时导出每会话 2 个、宿主 4 个，每次最多 60 秒；文件上限取 VM、文件服务和 100 MiB 中较小者。拒绝空文件、目录、软链接、特殊文件及路径穿越。停止、停用或凭据撤销会取消正在进行的导出/上传，已被平台接受的真实回执仍保留。

### 程序只读工具

VM 令牌可 `POST /vm/history`（例如 `{"query":"TEST"}`）、`/vm/web-search`（`{"query":"TEST","freshness":"week"}`）及 `/vm/web-fetch`（`{"url":"https://example.test","offset":0}`）。参数与对应宿主工具一致，不接受 scope、actor 或 API key。

只读入口在 `/stop` 和程序写动作停用期间仍可用，也不需要 sender；历史固定到物理会话，网页仍只允许公网目标，搜索凭据只由宿主使用。每会话最多 4 个、宿主合计 16 个并发，满了返回 429；请求体最多 16 KiB，工具等待上限 60 秒，断开客户端或撤销 VM 凭据会取消在途读取。参数错误返回 400，工具被禁用返回 403，网页/搜索失败返回 502。

### 程序消息流

`GET /vm/events?after=<内部消息ID>` 返回 `application/x-ndjson`，每行字段为 `id`、`at`（北京时间 RFC 3339）、`sender`、`person`（内部人员编号）、`reply_to`、`text`；实际出站另有 `by: "bot"` 或 `"program"`。原生 openid/消息索引和原始事件不会作为字段输出，命令及被 clear 隐藏的记录也不进入流。

省略 after 时，响应头 `X-Gaoji-Head` 是订阅起点，之后只发送新消息；带 after 时按每页 100 条回补并接着读取新消息，不静默截断长回补。30 秒无消息发送空行心跳。客户端保存最后完整收到的 id，断开后从它重连；尚未收到消息时用响应头中的起点。游标不能为负数或超前。

每会话最多 2 条、宿主合计 16 条订阅，超额返回 429。stop 不关闭读流；VM 凭据撤销或轮换会让流以传输错误结束，不能当作成功读完。常驻单元管理仍在实施；以上入口只做过本地替身验收，未部署。

### VM CLI

镜像工具定义已包含无第三方依赖的 `gaoji`，旧 VM 需 owner 执行显式工具升级。支持 say、wake、send-file、history、web-search、web-fetch、events、status；`gaoji --help` 查看参数。每次请求重读 `/run/gaoji` 凭据，不读环境代理，不跟随重定向。

写动作可指定 `--program`（默认 GAOJI_PROGRAM 或 manual）和 `--request-id`。自动生成的 ID 会先写 stderr；超时或断线不要换 ID 重发，使用 `gaoji status --request-id <id>`。`gaoji events --after <id>` 保留空行心跳，断流非零退出并在 stderr 报告可恢复游标；只将完整消息行交给调用者。

## GitHub 机器人账号

owner 自行注册专用账号，并在宿主配置两个 API；仅用 `public_repo` classic token，不给私有仓库或删除仓库权限。不将 token、basic-auth 编码、私人邮箱带进 VM。以下是合并到上述 host JSON 的示例片段，均为测试占位值：

```json
{
  "github": {"login": "TEST-BOT", "user_id": 12345},
  "apis": {
    "github": {
      "base_url": "https://api.github.com",
      "secret_file": "/run/secrets/TEST_GITHUB_TOKEN",
      "headers": {
        "Authorization": "Bearer {secret}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TEST-BOT"
      },
      "per_hour": 100,
      "about": "公共仓库 REST API。只在用户明确要求时开 PR，描述末尾单独写 This pull request was opened by an AI at a group member's request. 不发送垃圾内容。"
    },
    "github-git": {
      "base_url": "https://github.com",
      "secret_file": "/run/secrets/TEST_GITHUB_BASIC",
      "headers": {"Authorization": "Basic {secret}", "User-Agent": "TEST-BOT"},
      "per_hour": 100,
      "about": "HTTPS git 推送；只使用机器人账号获准访问的公共仓库。PR 仍须用户明确要求并注明 AI 身份。"
    }
  }
}
```

`TEST_GITHUB_BASIC` 文件保存 `base64("x-access-token:<同一个 token>")` 的单行值，不是原 token；两个私有文件必须一起轮换。实际值由 owner 在宿主生成，不能让 VM 获取它们。每会话每小时 100 次仅是示例配置；Git 协议的多次 HTTP 请求分别计数。

`github.login` / `user_id` 只保存公开提交身份。会话 VM 创建、启动和后台任务接管已运行的 VM 时，宿主通过 guest agent 写 `/etc/gitconfig`，邮箱为 `12345+TEST-BOT@users.noreply.github.com`；设置失败明确中止，不猜私人邮箱。模板和非会话 VM 不写此身份。宿主不强制审查 PR 正文，用户明确要求与 AI 署名规则在提示词和 `skills/github.md` 说明。

没有在本地测试中连接真实 GitHub 或创建 PR；账号、token 权限及真实推送由 owner 测试环境验收。

旧的项目授权、迁移路线、Wiki、主机写操作策略已移除。管理员后台只读运维另用 `AI_OPS_READ_BASE_URL` 和 `AI_OPS_READ_TOKEN_FILE`；token 只在宿主读取。
