# QQ 官方受保护宿主装配

本文描述当前 M8 候选集成使用的宿主配置；发布、CI 和真实验收状态见 [实施进度](qq-official-progress.md)。它选择现有应用服务，不代表生产迁移、反代部署或真实大文件验收已完成。

新配置入口为 `AI_QQ_OFFICIAL_HOST_CONFIG_FILE`；只有 `AI_QQ_OFFICIAL_ENABLED=true` 时才读取该路径。官方关闭且未启用旧迁移配置时，不读取宿主文件或官方凭据。官方启用但未指定宿主文件，不会自动启用文件或后台策略。

兼容旧 M5 部署的 `AI_QQ_MIGRATION_ENABLED` 默认仍为 `false`；设为 `true` 时，`AI_QQ_MIGRATION_CONFIG_FILE` 必须非空。这一旧入口可在官方连接关闭时继续为既有 OneBot 受管来源提供迁移边界，不会因此启动官方连接。若旧入口与新入口同时生效，两者必须指向同一配置路径；即使内容相同，不同路径也会以配置冲突拒绝启动。切换至 Nix `qqOfficial.hostConfigFile` 的 systemd credential 路径时，应同步处理旧环境变量，不能保留另一个旧路径。

文件必须是受保护的绝对路径 JSON，最大 16 KiB；使用现有凭证读取器的所有者、权限、有效 ACL 和拒绝符号链接检查。格式错误、未知字段或重复键拒绝启动，不在错误中输出配置内容。Nix 使用 `services.gaoji.qqOfficial.hostConfigFile` 传入 Nix store 外的字符串路径，由 `LoadCredential` 加载；不把文件内容写入 Nix 配置。

该文件只选择现有宿主服务。它不能携带 Python 模块路径、账号密钥、管理员映射、项目 grant 或验证码。新群绑定、身份映射、项目授权和逐项迁移仍使用原控制面，加载配置不会创建迁移任务。已有受管路由而未配置宿主时拒绝启动，防止旧 worker 绕过迁移状态。

App 向导只生成规范账号、空 `onebot_accounts` 和可选 `files`；它不会填写旧来源、媒体或后台模型策略。其他字段由可信部署者手动配置，不能把手动文件与 App 管理的不可变运行版本混写。最小格式如下，账号值仅为示例，须由实际受信部署记录替换；此例不包含任何迁移来源或文件验收证明：

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP\",\"production\"]",
  "onebot_accounts": []
}
```

`account` 必须与实际官方凭证构造的账号及环境完全一致。`onebot_accounts` 是必填数组，单平台可为空；最多 16 条，账号不得重复。每条实际来源含完整规范 `account` 和审阅的 `deployment_digest`。不能从当前连接列表任选 bot，也不能把占位摘要或新随机值当成既有部署证据。启用要求既有 PostgreSQL、投递和管理员安全服务可用。

可选字段：

| 字段 | 含义 |
|---|---|
| `scheduled_profile`、`subagent_profile` | 既有模型目录中的明确 profile；启动时校验。省略则不装配相应续接域。 |
| `deadlines` | 缺少原始期限的逐项审阅政策。每项指定 `source_endpoint`、`kind`、`reference`、绝对 Unix 秒 `execution`/`result`，可带领域校验的 `schedule_json`。不能用重启时的当前时间延长期限。 |
| `files` | 私有 `private_directory`、`native_routes`，以及 `FileServiceConfig` 的生成开关、容量和文件上限。未配置就不装配文件服务。 |
| `host_services` | 每项明确 `host_id`、`resource_ref`、`backend_ref`、`backend_binding_version`、`actions`。这只限定可见服务；每次宿主操作仍需要实际来源和独立管理员批准。省略时无宿主工具。 |
| `media` | 显式媒体策略，省略时不装配官方媒体服务；字段与当前 `MediaConfig` 一致，不接受传输回调。 |
| `native_background` | 可选 `schedules`、`proactive`、`wiki` 对象；各自默认不启用，启用项必须指定已有模型 `profile`，并按各自策略校验期限、成本和来源证据。与上面的迁移续接 profile 分开。 |

`files` 当前支持图片 `file_type: 1` 与普通文件 `file_type: 4` 的原生路线，以及可选 `download` 后端。PDF/ZIP 分片另需目标记录明确 `multipart=true` 和独立 `files.multipart` 主机策略；旧证明不会自动升级。完整字段、默认值、大小、128 条路线限制、契约和空路线示例统一见 [文件配置说明](qq-official.md#文件能力记录)。旧 `contract` 保持原值，运行时不匹配就拒绝；不能因代码通过假平台测试便生成新证明。

省略 download 时没有下载监听器。配置后只绑定明确 loopback 地址，经已有可信 HTTPS 反代访问；不会自动部署或开放公共代理。App/Docker 使用 `/data/state` 下的私有目录，Nix 使用实际 `StateDirectory` 下的服务用户可写目录，不能直接照抄容器路径。宿主配置也不会改变已有链接、产物或任务的原期限。

现有应用启动按账号租约装配迁移、审批、文件和 Sub-Agent 服务。项目运行时实际就绪后，再装配项目 provider、派发和文件桥；账号租约重建时重新绑定当前服务。控制台复用原 cookie/CSRF 与数据库授权。M8 候选已接官方单平台入口和 Nix/桌面配置；它们的本地测试、发布状态与实际部署验收分别记录，不能相互替代。

迁移验证使用现有管理员会话的 `POST /bot-admin/api/v1/qq-official/migrations/{migration_id}/verification`。请求提供当前已审阅的 `preview_digest`、`kind`（`text` 或 `file`）及实际原入站的 `inbox_id`；仅 `file` 同时提供已保存产物的 `artifact_id`。后端核验原操作者、当前 owner、原回复窗口和固定期限，客户端不能提交 nonce、平台消息 ID 或成功回执来冒充验证。文字回复返回原发送计划 ID；文件回复 `awaiting_file`，由既有文件工作器处理。重复请求复用原计划与原期限。

先确认新目标的文件能力实际可用，再创建 `planned` 迁移，然后让目标 owner 通过真实普通消息生成一份小文件。计划阶段仍允许正常生成；该迁移目标中尚未尝试上传或发布的文件会由原工作器等待，不消耗重试次数。完成预览、冻结、排空、切换并进入 `verifying` 后，使用该文件的原入站和产物登记验收，原工作器即继续处理。等待不延长文件、迁移或回复窗口的原期限；已尝试或结果不明的文件保持原恢复规则，不被重新暂停或补发。

不要用清空 `native_routes` 的方式暂停文件：没有原生或链接能力时，正常文件生成入口也不可用。已经发送的旧 PDF/ZIP 不可充当本次尚未发送的验收产物；必须在新计划下准备真实文件，并在原有效窗口内完成验收。

运行时从实际已应用且 committed 的回执自动登记文字和文件验证；两类证据齐全后，管理员才能请求完成迁移。平台接受文件消息仍不等于用户下载成功。迁移 grant 的显式重授保留原 grant ID 并递增 revision，要求精确 `expected_revision`，只用于尚未开始或 planned 的迁移；旧任务和文件继续绑定旧版本，不会获得新授权。M7 已接迁移完成后的项目授权命令：由当前管理者明确授予当前接收者操作范围与期限，仍核对已审阅资源来源；不会复活旧 grant、旧版本或过期身份映射。
