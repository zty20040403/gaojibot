# QQ 官方接入：M1 边界核对

2026-10-28，基于 `pre-qq-official` / `af0fba6` 和 M0 合并后的实现核对。下列 M1 路径已实现并做定向测试；最终全量回归和 CI 在实施进度中单独记录。本文不代表真实 QQ 或后续里程碑已验收。

## 必须保留的共享路径

OneBot 回合仍由 `ReplyService._finish_tracked_ai` 经 `OneBotChatService._run_tracked_ai`、`ChatOrchestrator.run` 到 `ToolExecutor._ask_ai`。后者将真实事件转换为结构化输入，调用 `application/shared_agent_turn.py` 的上下文准备、提示词构建、真实 agent loop 和记忆写回。

官方可信入站由 `build_platform_chat_orchestrator` 组合 `PlatformChatService`，使用同一个 orchestrator 和上述四段共享核心。新进程阻断全部 NoneBot 导入后，实际 PostgreSQL、工具调用、个人记忆、历史和 journal 的整条路径通过测试。入站接收与授权尚由 M2 接线；Sub-Agent 尚由 M4 接线，当前不能开启。

## 当前耦合和处理归属

| 位置 | M1 实际处理 | 剩余归属 |
| --- | --- | --- |
| `application/chat_orchestrator.py` | 接收 inbound/platform；记录与任务使用完整身份；实际 endpoint 决定开关和群默认 | M2 入站准入；M3/M4 发送与持久派发 |
| `tasks.py` | 官方注册、查询、反馈与取消按 endpoint+actor；旧数字接口不命中官方任务 | M4 持久任务 |
| `tool_executor.py` | `_ask_ai` 保留真实 OneBot 包装；共享核心移到 application；官方服务无需 legacy event | 媒体及专用工具分配到 M6/M7 |
| `tool_executor.py` 的任务派发 | requester 是整数；dispatch 包含 `event.model_dump()` 和 `bot.self_id` | 结构化平台 dispatch，不能将 principal ID 冒充 QQ 号；持久任务授权与续接在 M4/M5 收口 |
| `tools/state.py` | 中立上下文不导入 SDK；OneBot checkpoint 显式注入专用 codec | 官方任务续接 M4 |
| `tools/context.py`、`memory.py` | 官方读取可信 history scope，个人记忆按 actor.key；群记忆写入默认拒绝；执行与工具列表双重检查 | reminders M7；成员证据/项目授权 M5 |
| `tools/media.py`、`stickers.py` | 未移植工具在官方列表隐藏且执行拒绝；旧 OneBot 分支保持 | M6/M7 |
| `ledger.py` | 0061 增加账号/身份命名空间；消息保存 actor/source endpoint；引用、去重按实际端点；bot 作者独立且不能作为请求主体 | M2 从认证平台事件确认真实 bot ID；M3 成功回执后出站落账 |
| `long_term_memory.py`、`context_pipeline/recall.py` | 官方 actor provenance 与 nullable legacy QQ 列；回忆不按裸 openid 匹配 | M5 历史身份关联与撤权 |
| `model_preferences.py`、`config.py` | 新 scope 入口兼容旧 key；官方默认关闭；实际群配置与 canonical history 分开 | M2/后续后台配置接线 |
| `conversation_keys.py`、`trigger_service.py`、`admin_api/scoped_model_views.py` | 统一键解析；后台按实际群显示官方设置，同 actor 不同 history 保留独立 turn_key | OneBot 专用数字正则保留，不能用于官方权限 |
| `project_access.py`、`project_runtime.py` | Event 类型、数字 ID、`get_bots`、实时成员 API | M5 独立可撤销 grant，任务恢复/执行/交付重新检查 |
| `project_file_transport.py`、`agent/background.py` | 原 bot、群文件回执、平台 dispatch | M6 文件路线，M5 项目授权；OneBot 旧传输保持原样 |
| `adapters/__init__.py` | 懒导出；公共类型实际位于平台中立模块，adapter 路径兼容重导出 | 无隐式平台初始化 |
| `ai_chat/__init__.py`、`onebot_plugin.py` | 包导入无副作用；显式插件保留 34 matcher、1 preprocessor 与原生命周期 | M2/M4/M8 官方独立生命周期与最终三种启动模式 |
| `application/commands.py` | 帮助/绑定/模型/effort/用量/任务状态取消公共分发；配置权限默认拒绝，OneBot callback 保留原参数和 finish 信号 | M2 binding service，M4 业务接线，M5/M7 其他命令 |

## 验证门槛

- `test_onebot_platform_snapshots.py` 固定时钟、假身份、模型结果和平台 I/O，比较真实出站路径的最终 API 参数。包含群/私聊、引用、mention、图片、长回复、Sub-Agent、提醒与项目文件。快照生成于业务重构之前，不自动更新。
- 官方最小回合使用非数字身份和 `legacy event=None`，进入同一个共享核心；把所有旧 helper 设为一旦调用就失败，证明没有暗中走 OneBot 分支。
- 相同字面 openid 在不同账号、环境、群、C2C 中分别测试历史、mention、任务取消和记忆权限。
- 公共类型、端口做 AST 边界扫描；另以干净子进程阻止 OneBot 导入，检查包初始化的传递依赖，不能只看单文件 import。
- M5/M6/M7 尚未接通的工具必须隐藏且宿主执行拒绝。不得用临时假 QQ 号或假的 OneBot Event 通过测试。

对应测试包括 `test_shared_agent_turn`（含干净进程真实 composition）、`test_platform_private_reply_context`、`test_platform_chat_failures`、`test_platform_identity_storage/migration/outbound_identity`、`test_platform_preferences/tasks/commands/context_tools/tool_gates/import_boundaries` 和原 OneBot 快照。`Delivery.target_scope` 仍是旧字段，官方 outbox 不得在 M3 补齐前启用。
