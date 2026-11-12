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

任务与 VM 调用共用按物理会话的 API 限额；审计分别标为 `job#…` 和 `vm:…`。`/stop` 取消任务身份，但按方案保留 VM 的 `/api` 能力，不停掉 VM；待实现的 `/vm/say`、`/vm/wake`、`/vm/send-file` 会另外受停止门槛限制。此处的 VM 凭据接线不代表常驻单元/发言/唤醒功能已经完成。

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

### GitHub 机器人账号

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
