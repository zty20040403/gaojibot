# 本地千问：健康路由与受限启停

## 两条独立通道

```text
聊天 -> LLMGateway -> 只读内存健康结果 -> Qwen 或 DeepSeek
                           ^
                  h610 后台探测 /v1/models

浏览器 -> 认证的 Admin API -> WSL 管理 API -> 固定 Qwen systemd 服务
         版本检查、审计      Token + 来源 IP     只有 start / stop
```

健康探测不依赖控制台是否打开，也不会启动千问。Bot 重启后先跳过千问，
首次探测确认目标模型可用后才接管。默认探测间隔 15 秒，单个接口超时 2 秒；
状态过期后也跳过。检测间隔内发生故障仍可能遇到一次实际请求失败，由原有
超时和降级机制处理。`/models` 就绪不保证所有推理请求都成功。
本地千问设置 `circuit_breaker_enabled = false`，推理失败仍记录错误并降级，
但不进入熔断冷却；下一次请求在健康探测就绪时可以立即重试千问，方便调试。
接口离线或探测结果过期时仍跳过。其他模型默认保留熔断。

## h610 配置

先确认 `AI_MODEL_PROFILES_JSON` 存在以下 profile，保留已有其他 profiles：

```json
{
  "qwen-local": {
    "provider": "qwen",
    "protocol": "openai-chat",
    "base_url": "http://wsl.inner.imdomestic.com:8000/v1",
    "model": "qwen3.8-27b",
    "api_key_required": false,
    "thinking": "enabled",
    "circuit_breaker_enabled": false,
    "timeout_seconds": 30,
    "fallback_profiles": ["deepseek", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
  }
}
```

`provider = qwen` 使用 NInfer Chat Completions 的 `enable_thinking` 开关：
`enabled` 发送 `true`，`disabled` 发送 `false`，`auto` 不传开关、使用服务端默认值。
这与 GPT 的 `reasoning_effort = xhigh` 档位不同。开启思考可能增加回复时间和
Token 用量；群里只发送最终答案，不发送 `reasoning_content`。
接口约定见 [NInfer serving 文档](https://github.com/Neroued/ninfer/blob/master/docs/serving.md)。

上下文窗口与单次输出上限不同；思考 Token 也占单次输出额度。千问返回只有思考、
没有正文（或空正文且 `finish_reason = length`）时，Bot 会记录失败原因，并最多
补救两次：先仅为本次补答关闭思考，仍无正文再排除该模型、走配置中的备用链。
每次补救最多等待 60 秒、最多输出 4096 Token，并保留更小的原请求输出限制。
补答保留已有工具结果，但禁止新工具调用；不会把未完成思考当作答案发送，
也不会改变正常请求的思考配置。已有正文、正常工具调用和内容安全拒绝不走此恢复。

Bot 环境变量：

```dotenv
AI_SIMPLE_CHAT_PROFILE=qwen-local
AI_LOCAL_MODEL_PROFILE=qwen-local
AI_LOCAL_MODEL_PROBE_INTERVAL_SECONDS=15
AI_LOCAL_MODEL_PROBE_TIMEOUT_SECONDS=2
AI_MODEL_FALLBACK_ENABLED=true
AI_QWEN_CONTROL_URL=http://wsl.inner.imdomestic.com:8001
```

健康路由只需模型配置，管理 API 未部署时也可以工作。群、个人和引用消息的
明确模型选择优先于简单聊天默认模型；选中离线千问时仍执行可用性降级。
`AI_LOCAL_MODEL_PROFILE` 为空会关闭此专用健康检查。

## WSL 控制服务

机器人 flake 导出 `nixosModules.qwen-control`。在 nix-config 更新机器人
input 后，将此 module 加入 WSL 的 `externalModules`：

```nix
inputs.qq-bot.nixosModules.qwen-control
```

WSL 配置：

```nix
services.gaoji-qwen-control = {
  enable = true;
  listenAddress = "192.0.2.10"; # Replace with the model server's private address.
  port = 8001;
  allowedPeers = [ "192.0.2.11" ]; # Replace with the bot server's private address.
  tokenFile = "/var/lib/gaoji-secrets/qwen-control-token";
};
```

服务只允许管理固定的 `podman-qwen38.service`，不会改变容器原有的
`autoStart = false`。控制服务启动不等于启动模型。若启用了防火墙，只允许
h610 的 Tailscale 地址访问 8001；不要将该端口暴露到公网。HTTP 控制流量应
仅经过 Tailscale 加密内网；其他网络必须在管理 API 前加 TLS。

准备一段至少 32 字符的随机 Token，存入 WSL 上述 root 拥有、权限 0600 的
文件，并将同一值设置为 h610 私密环境文件中的 `AI_QWEN_CONTROL_TOKEN`。
管理台使用独立的 [账户密码与 QQ 手机授权](account-security.md)，启停模型也需要 6 位一次性口令。
服务凭据不要提交到 Git 或写进 Nix 表达式：WSL 使用 `LoadCredential` 读取，不把 Token 放进 Nix store。

管理 API 以无特权用户运行；Polkit 只授权此用户启停这一个 unit。
`GET /status` 只读取 systemd 和 nvidia-smi；`POST /start`、`POST /stop`
仅接受 `{"request_id":"UUID"}`。不存在命令、路径或服务名称输入参数。
请求 UUID 在 WSL 本机 SQLite 中去重，独立于 Bot PostgreSQL；因此管理服务
重启也不会重放已接受的启停命令。未知结果不自动重试。

这些是部署配置步骤，本地编译或测试不会执行任何服务器 switch。共享主机
部署前仍需先 `git fetch origin`，确认没有遗漏同学的更新，再更新机器人
flake input、检查构建结果，并在批准后分别 switch WSL 与 h610。

## 控制台含义

- 已关闭：systemd 确认服务停止。没有管理接口时只显示“接口不可达”，不假装知道原因。
- 启动中：已提交启动请求或 systemd 正在启动。
- 模型加载中：推理接口未就绪，或 `/models` 中没有目标模型。
- 已开启：接口可用且目标模型就绪；本地千问关闭熔断，不因上次推理失败而冷却。
- 显存：WSL GPU 整体占用，不是千问独占占用；缺失显示“暂无数据”。
- 请求数与平均首响应：本次 Bot 进程累计，只统计实际发送的推理请求。
  流式响应计到返回流对象为止，不包含完整生成时间，不等于整轮回复耗时。
- 启停：请求“已接受”不表示操作已完成，后续健康探测更新状态；停止可能中断
  已经生成中的回答。版本冲突拒绝覆盖；操作记录在审计页。

Trace 页每轮保存每次模型请求的期望模型、实际模型、候选跳过/失败原因。
“查看全部”里可展开每次路由决策。没有成功响应时明确显示“未成功响应”。
旧 Trace 没有此字段的只显示原有记录，不追溯编造历史降级原因。
