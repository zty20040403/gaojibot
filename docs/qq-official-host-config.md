# QQ 官方 M5 宿主装配

M5 本地完整回归已通过，里程碑 PR/CI 和实际迁移验收单独记录。此说明描述显式宿主装配接口，不代表生产迁移已启用。

实际应用启动入口读取 `AI_QQ_MIGRATION_ENABLED`，默认 `false`。关闭时不读取配置文件。开启后，`AI_QQ_MIGRATION_CONFIG_FILE` 必须指向受保护的绝对路径 JSON 文件，最大 16 KiB；使用现有凭证文件的所有者、权限、有效 ACL 和拒绝符号链接检查。格式错误、未知字段或重复键拒绝启动，不在错误中输出配置内容。

该文件只选择现有宿主服务。它不能携带 Python 模块路径、账号密钥、管理员映射、项目 grant 或验证码。新群绑定、身份映射、项目授权和逐项迁移仍使用原控制面，加载配置不会创建迁移任务。已有受管路由而未配置宿主时拒绝启动，防止旧 worker 绕过迁移状态。

最小格式如下，所有账号值均为示例，须由实际受信部署记录替换：

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP\",\"production\"]",
  "onebot_accounts": [
    {
      "account": "account:v1:[\"onebot-v11\",\"12345\",\"production\"]",
      "deployment_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ]
}
```

`account` 必须与实际官方凭证构造的账号及环境完全一致。OneBot 账号必须明确，不能从当前连接列表任选一个 bot。`deployment_digest` 是审阅的部署版本摘要，不是占位值或启动时生成的新随机值。启用要求既有 PostgreSQL、投递和管理员安全服务可用。

可选字段：

| 字段 | 含义 |
|---|---|
| `scheduled_profile`、`subagent_profile` | 既有模型目录中的明确 profile；启动时校验。省略则不装配相应续接域。 |
| `deadlines` | 缺少原始期限的逐项审阅政策。每项指定 `source_endpoint`、`kind`、`reference`、绝对 Unix 秒 `execution`/`result`，可带领域校验的 `schedule_json`。不能用重启时的当前时间延长期限。 |
| `files` | 私有 `private_directory`、`native_routes`，以及 `FileServiceConfig` 的生成开关、容量和文件上限。未配置就不装配文件服务。 |
| `host_services` | 每项明确 `host_id`、`resource_ref`、`backend_ref`、`backend_binding_version`、`actions`。这只限定可见服务；每次宿主操作仍需要实际来源和独立管理员批准。省略时无宿主工具。 |

`files.native_routes` 中每项保存完整 `endpoint`、`endpoint_generation`、`route_epoch`、`capability_revision`、`max_bytes`、`verified_at`、`expires_at`、显式 `contract` 和整数 `file_type: 4`。旧 `contract` 保持原值，代码升级不会替证据换约；运行时发现契约不同就拒绝原生发送。这些是受控验收后审阅的原始证据，不能因代码已通过假平台测试而填写为已验证。运行时与当前端点逐项比较，原绝对截止到期即失效。此入口只提供原生文件路线；没有配置公开链接监听器，也不会自动扩大平台权限。

现有应用启动按账号租约装配迁移、审批、文件和 Sub-Agent 服务。项目运行时实际就绪后，再装配项目 provider、派发和文件桥；账号租约重建时重新绑定当前服务。控制台复用原 cookie/CSRF 与数据库授权。独立无 OneBot SDK 的产品入口及 Nix/桌面配置界面在 M8 收口，不由本文宣称已完成。

迁移验证使用现有管理员会话的 `POST /bot-admin/api/v1/qq-official/migrations/{migration_id}/verification`。请求提供当前已审阅的 `preview_digest`、`kind`（`text` 或 `file`）及实际原入站的 `inbox_id`；仅 `file` 同时提供已保存产物的 `artifact_id`。后端核验原操作者、当前 owner、原回复窗口和固定期限，客户端不能提交 nonce、平台消息 ID 或成功回执来冒充验证。文字回复返回原发送计划 ID；文件回复 `awaiting_file`，由既有文件工作器处理。重复请求复用原计划与原期限。

运行时从实际已应用且 committed 的回执自动登记文字和文件验证；两类证据齐全后，管理员才能请求完成迁移。平台接受文件消息仍不等于用户下载成功。迁移 grant 的显式重授保留原 grant ID 并递增 revision，要求精确 `expected_revision`，只用于尚未开始或 planned 的迁移；旧任务和文件继续绑定旧版本，不会获得新授权。迁移完成后的项目授权命令待 M7 接通。
