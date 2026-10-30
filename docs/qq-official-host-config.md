# QQ 官方宿主配置

S1–S8 简化版使用 `AI_QQ_OFFICIAL_HOST_CONFIG_FILE`。官方接入启用时读取；兼容旧 `AI_QQ_MIGRATION_ENABLED` / `AI_QQ_MIGRATION_CONFIG_FILE`，新入口优先。Nix 继续用 `services.gaoji.qqOfficial.hostConfigFile` 和 systemd `LoadCredential`，凭据放在 Nix store 外。

配置文件使用 JSON。读取器检查普通文件及非 world-readable 权限，例如 0600 或服务组可读的 0640；不再检查 ACL、文件系统白名单或 SDK 证明。日志不输出配置正文、凭据或路径。整个文件无法读取时关闭宿主扩展；某项可选配置有误时只关闭该功能并记录 warning，基本聊天继续工作。

以下使用虚构账号；部署时填写实际账号键：

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP\",\"production\"]",
  "files": {
    "private_directory": "/var/lib/gaoji/official-files",
    "generation_enabled": true
  },
  "media": {"asr": true, "documents": true, "forwards": true, "emoji": true}
}
```

`files` 启用存储和原生上传；`generation_enabled` 启用会话沙盒及文件工具。沙盒后端沿用应用配置。默认单文件上限 100 MiB；5 MiB 以内 base64，超过后分片上传。任何带文件名的格式均可发送，图片用类型 1，其他文件用类型 4。旧 `native_routes`、`multipart`、`download` 字段忽略，不再要求证明或部署公共下载服务。

可选 `native_background` 包含 `schedules`、`proactive`、`wiki`。启用的功能须指定可用模型 profile。例行任务保留现有执行时限与成本预算；Wiki 的收集策略保持独立。一个后台功能配置有误，不影响其余功能。可选 `host_services` 仍是允许执行的主机服务清单；主机操作需 owner 私聊验证码确认，不会因加载配置而执行。

旧 `onebot_accounts`、`deadlines`、迁移模型等字段不再参与运行。历史接入改成 owner 私聊 `/bind <老群号>` 获取六位码，然后在目标群 @ Bot 发送 `/bind <码>`。没有旧历史时可私聊 `/bind create`。提交后立即绑定并关联群内 owner，新消息和发送回执沿用原时间线；已有绑定、项目和审批账户保留。

普通回合直接运行。发送器默认主动发送，腾讯拒绝主动发言时使用最近消息窗口；超过窗口时明确报错。重启后不重跑普通回合，会通知用户重发；Sub-Agent、提醒、例行任务和待发结果继续使用持久任务。

INFO 日志记录入站类型、脱敏会话标识、是否 @、回合耗时/实际模型/工具次数，以及发送和上传结果，不含消息正文。官方回合计入 `gaoji_ai_turns_total` 和 `gaoji_ai_turn_duration_seconds`，标签 `platform="qq-official"`。上线状态与分阶段记录见 `qq-official-simplify-plan.md`；S9 的旧表清理须等新版稳定一周。
