# QQ 官方适配：17 条不变量的测试映射

> 下列内容包含简化前的设计与验收记录。S1–S8 的当前行为以 [简化方案](qq-official-simplify-plan.md) 和 [宿主配置](qq-official-host-config.md) 为准；旧迁移证明、ACL 和回复窗口限制已移除。

本页对应[方案第 5 节](qq-official-plan.md#5-不变量防-bug-的底线)，供 M9 逐项核查。测试名和断言按集成源码核对，链接均相对仓库。它是证据索引，不是“所有测试已通过”的声明；定向批次、各启动形态的完整回归、CI 和人工验收的实际结果分别见[进度记录](qq-official-progress.md)。文档更新本身不代表重新执行测试。

证据有不同层次：纯测试验证策略和序列化；本机 PostgreSQL 验证事务、锁、持久状态和恢复；FakePlatform/loopback HTTP、WS、TLS 验证真实传输边界，但服务端仍是假平台。fake LLM、替身管理器或注入时钟不证明真实模型、平台权限或客户端显示。以下每节只描述所列断言覆盖的范围。

## 1. 旧路径隔离

官方功能关闭且会话未迁移时，旧 OneBot 行为保持原样。旧群回复和群/私聊提醒比较实际发送调用的固定快照；实际旧平台启动确认官方关闭时不读取官方凭据。双平台测试在同一 AppContext 中运行真实 builder、OneBotRuntime 和 OfficialRuntime，检查共享存储、两端入站、worker 不重复及关闭顺序；公共 worker 单元测试另检查官方单平台不会误启动旧投递、审批和视觉 worker。

对应测试：

- [OneBotPlatformSnapshots.test_ordinary_group_reply](../tests/test_onebot_platform_snapshots.py)
- [OneBotPlatformSnapshots.test_group_and_private_reminders](../tests/test_onebot_platform_snapshots.py)
- [PlatformModeTests.test_native_host_path_is_dormant_when_official_disabled](../tests/test_official_application_startup.py)
- [CommonWorkerTests.test_official_common_workers_never_start_legacy_delivery_approval_or_vision](../tests/test_official_application_startup.py)
- [DualApplicationTests.test_real_legacy_only_context_never_opens_official_credentials](../tests/test_dual_application_postgres.py)
- [DualApplicationTests.test_real_dual_context_owns_one_worker_set_two_ingresses_and_ordered_shutdown](../tests/test_dual_application_postgres.py)

覆盖边界：快照只覆盖其固定场景，公共 worker 单元测试使用替身服务。新增生命周期测试使用真实 PostgreSQL 和 runtime，官方网络接本机 FakePlatform，OneBot 实际 Bot dispatch 的 API 边界返回假回执；它们不替代完整 OneBot 兼容回归或真实 NapCat/QQ 部署验收。

### 三种启动形态的证据矩阵

默认配置保留 OneBot、关闭官方入口；启用官方单平台或双平台必须显式选择。下表区分完整回归形态和定向证据，批次结果、CI 状态与执行环境均以[进度记录](qq-official-progress.md)为准。

| 启动形态 | 具体证据 | 覆盖范围与边界 |
| --- | --- | --- |
| 默认官方关闭、OneBot 启用 | [PlatformModeTests.test_default_and_dual_preserve_onebot](../tests/test_official_application_startup.py)；[DualApplicationTests.test_real_legacy_only_context_never_opens_official_credentials](../tests/test_dual_application_postgres.py) | 默认开关及实际旧平台 builder、assembly、worker、ingest、关闭；官方凭据读取哨兵从未触发。该形态的完整兼容回归结果单独记录，不以这两条定向测试代替全量。 |
| 官方单平台（SDK-blocked 验证） | [PlatformModeTests.test_full_product_asgi_import_has_no_nonebot](../tests/test_official_application_startup.py)；[NativeApplicationTests.test_real_epoch_zero_legacy_baselines_projects_security_reconnect_shutdown](../tests/test_official_application_postgres.py) | 禁止导入 NoneBot 的独立进程检查真实产品入口；实际 PG/Fake 生命周期检查零 legacy baseline、项目/安全服务、命令、重连、健康与关闭。实际生命周期的 SDK-blocked 执行批次见进度记录；普通运行该方法本身不自动安装导入禁令。 |
| 双平台同时启用 | [DualApplicationTests.test_real_dual_context_owns_one_worker_set_two_ingresses_and_ordered_shutdown](../tests/test_dual_application_postgres.py) | 真实 OneBotRuntime/OfficialRuntime 共用 context、ledger、journal 和 pool；无 OneBot 连接时官方仍 ready，两端分别入账，官方 `/help` 经 M3 到 committed。并发重复 start 不复制共享 worker/健康 writer；官方先停，再关闭共享存储，fixture 独立 pool 不被误关。实际平台 I/O 为 FakePlatform/假 Bot 边界，未据此声称真实双平台部署通过。 |


## 2. 入站可靠接管

去重、入站正文、恢复点和 ingest job 在事务中接管；后续入账与 turn intent 并发去重。测试在实际 enqueue 后、checkpoint 更新时注入故障，确认整笔接管回滚；两个 worker 只得到一条消息和一个逻辑 job。runtime 恢复复用同一 inbox/job，不另建回合。

对应测试：

- [OfficialInboxTests.test_enqueue_failure_after_real_job_insert_rolls_back_entire_acceptance](../tests/test_qq_official_inbox.py)
- [OfficialInboxTests.test_checkpoint_update_failure_rolls_back_receipt_dedup_inbox_and_job](../tests/test_qq_official_inbox.py)
- [QQOfficialIngestTests.test_concurrent_workers_create_one_logical_turn](../tests/test_qq_official_ingest.py)
- [OfficialRuntimeTests.test_ingest_failure_pauses_and_resumes_same_durable_inbox_once](../tests/test_qq_official_runtime.py)

覆盖边界：这些是本机 PostgreSQL 事务、线程竞争及受控协议故障证据。“已持久接管后可恢复”不保证平台从未丢事件，也不把唯一 job 等同任意外部副作用 exactly-once。

## 3. 发送结果不明不重放

请求可能已被接受时保留 ambiguous/unknown，不自动换序号、重新发布链接或重传文件。sender 丢响应只产生一次物理发送；已取得回执但保存/结算失败时只恢复本地事实。错误策略不把裸 400、未知码或带已知数字码的未知接受状态改判为未发送。

对应测试：

- [OfficialSenderTests.test_dropped_response_is_ambiguous_and_never_replayed](../tests/test_qq_official_sender.py)
- [OfficialSenderTests.test_failed_capture_pauses_and_ram_recovery_never_resends](../tests/test_qq_official_sender.py)
- [OfficialSenderTests.test_finalize_failure_keeps_receipt_and_recovery_is_only_local](../tests/test_qq_official_sender.py)
- [DownloadCompositionTests.test_unknown_message_does_not_republish_resign_or_resend](../tests/test_qq_official_download_composition.py)
- [NativeChunksTests.test_unknown_put_never_restarts_session_or_falls_back](../tests/test_qq_official_native_chunks.py)
- [NativeChunksTests.test_unknown_merge_keeps_original_stage_no_new_session_or_m3](../tests/test_qq_official_native_chunks.py)
- [ErrorPolicyTests.test_unknown_timeouts_disconnect_5xx_and_missing_receipt_stay_ambiguous](../tests/test_qq_official_error_policy.py)
- [ErrorPolicyTests.test_numeric_code_never_overrides_ambiguous_acceptance](../tests/test_qq_official_error_policy.py)

覆盖边界：分片测试额外覆盖 PUT 和 complete 响应丢失：不重新 prepare、不重 PUT、不自动改链接，也不发 M3 消息。这是本机 FakePlatform 的调用次数证据，不是腾讯服务端故障实测。

## 4. 窗口原子分配

窗口额度、msg_seq、实际发送尝试与固定 wire payload 一起准备。六个并发申请最多获得五个窗口位置；事务失败不烧号，已释放的未发序号也不复用。旧入站重放保留原 expires_at/计数，拆分计划按 part 顺序发送。

对应测试：

- [OfficialSendPolicyTests.test_concurrent_window_claims_do_not_overspend_or_duplicate_sequences](../tests/test_qq_official_send_policy.py)
- [OfficialSendPolicyTests.test_seq_65535_burns_even_when_unsent_and_rollback_allocates_nothing](../tests/test_qq_official_send_policy.py)
- [QQOfficialIngestTests.test_idempotent_replay_does_not_refresh_window_or_create_another_turn](../tests/test_qq_official_ingest.py)
- [OfficialSenderTests.test_run_sends_split_plan_in_part_order](../tests/test_qq_official_sender.py)
- [OfficialOutboxTests.test_prepare_uses_existing_attempt_route_and_atomic_window](../tests/test_qq_official_outbox.py)
- [OfficialOutboxTests.test_prepare_rollback_never_burns_seq_or_part](../tests/test_qq_official_outbox.py)

覆盖边界：这里的并发与回滚使用真实 PostgreSQL，窗口/时钟为测试配置。payload 的一致性断言是持久 wire_json 等于保留的 reservation；不能由此推定平台实际窗口长度或额度。

## 5. 配额不因重启清零

配额账本按原周期持久存在，未验证日界不因换租约、重启或时间流逝而补满。已验证日界的未决 charge 与晚到接受保守跨期计入；服务端 cooldown 每次唤醒重新读取，等待时尚未建立发送 attempt。

对应测试：

- [OfficialSendPolicyTests.test_concurrent_daily_quota_claims_share_one_durable_budget](../tests/test_qq_official_send_policy.py)
- [OfficialSendPolicyTests.test_unverified_daily_limit_never_refills_on_restart_or_elapsed_time](../tests/test_qq_official_send_policy.py)
- [OfficialSendPolicyTests.test_verified_day_changes_preserve_old_charges_and_late_acceptance](../tests/test_qq_official_send_policy.py)
- [OfficialSenderTests.test_server_cooldown_is_reread_without_attempt_or_db_lock](../tests/test_qq_official_sender.py)

覆盖边界：跨日测试注入可信测试日界，不是腾讯日界实测。不得将主动发送额度泛化为全部消息额度；真实耗尽测试不在这些用例内。

## 6. 等待有类别和期限

持久结果遵守原绝对期限和默认 24 小时上限；unsolicited/progress 限短期，OTP 不晚发，迟到主动评分不能取得新期限。受保护诊断把已提交分片、未知分片及执行记录分开展示，并隐藏消息正文和原生标识。

对应测试：

- [PlanTests.test_normal_purposes_obey_default_wait_limit_and_preserve_absolute_deadline](../tests/test_qq_official_plans.py)
- [PlanTests.test_unsolicited_and_progress_have_short_deadlines](../tests/test_qq_official_plans.py)
- [OfficialSenderTests.test_otp_expired_unknown_or_wrong_endpoint_has_no_http](../tests/test_qq_official_sender.py)
- [ProactivePGTests.test_actual_slow_scorer_cannot_obtain_new_deadline](../tests/test_qq_official_proactive.py)
- [DetailReceiptTests.test_actual_parts_receipts_and_unknown_are_separate_from_execution](../tests/test_qq_official_diagnostics_details.py)

覆盖边界：这些测试检查存储/发送/API 数据，不证明页面视觉、所有平台消息缺口都已展示，或平台能补回断线缺口。执行期限、等待期限与 OTP 有效期必须分别解释。

## 7. 身份与数据隔离

身份使用完整 AccountKey/EndpointKey/ActorKey，拒绝整数、空值等隐式转换。同字面 openid 在 AppID、环境、群和身份类别间映射为不同 principal；合并历史 scope 不改个人 memory key，也不授予取消另一身份任务的能力。

对应测试：

- [PlatformKeyTests.test_numeric_and_blank_ids_are_not_coerced](../tests/test_platform_types.py)
- [PlatformIdentityStorageTests.test_same_opaque_id_isolated_by_app_environment_kind_and_namespace](../tests/test_platform_identity_storage.py)
- [PlatformMemoryTests.test_actor_memory_unchanged_by_canonical_binding_and_roundtrips_null_qq](../tests/test_platform_identity_storage.py)
- [PlatformTaskRegistryTests.test_cancel_never_crosses_endpoint_or_actor_despite_same_history](../tests/test_platform_tasks.py)

覆盖边界：所有身份为合成数据；没有推测或验证真实跨应用 OpenID 对应关系。历史范围和权限仍须单独授权。

## 8. 未绑定群不消费业务

未绑定普通群消息只保存最小安全接管元数据：无正文、昵称、URL、原文 hash、业务 job 或 ledger 消息。绑定命令在 TxA 消费，原码不持久化，重放不产生第二次 effect。私聊 owner/allowlist/public 与群绑定分别准入，平台 owner 字段不自动放行群业务。

对应测试：

- [OfficialInboxTests.test_unbound_ordinary_message_persists_no_body_nickname_url_or_content_hash](../tests/test_qq_official_inbox.py)
- [OfficialInboxTests.test_owner_private_allowlist_public_and_group_admission_are_distinct](../tests/test_qq_official_inbox.py)
- [OfficialInboxTests.test_binding_code_is_transient_and_replayed_command_returns_no_effect](../tests/test_qq_official_inbox.py)
- [WikiBoundaryTests.test_legacy_store_does_not_enable_official_group](../tests/test_qq_official_business_commands.py)

覆盖边界：群公开服务、体验名单和平台后台条件不由这些本地测试确认；配置允许接收事件也不等于本地业务已启用。

## 9. 首次身份可信

首次私聊和 whoami 不创建 owner。绑定候选固定真实提交者，竞抢、拒绝、重签不能沿用旧码授予攻击者。local_admin 只创建密码控制台身份；真正 C2C 绑定须同一登录 session、cookie/CSRF、实际入站码和显式确认，HTTP body 不能指定 openid/account/admin。

对应测试：

- [OfficialBindingTests.test_first_contact_does_not_enroll_owner_and_config_is_idempotent](../tests/test_qq_official_binding.py)
- [OfficialBindingTests.test_raced_code_freezes_actor_reject_reissue_cannot_grant_attacker](../tests/test_qq_official_binding.py)
- [NativeConsoleTests.test_local_bootstrap_login_never_creates_transport_or_owner](../tests/test_official_application_postgres.py)
- [IdentityHTTPTests.test_real_cookie_csrf_c2c_and_same_session_confirmation](../tests/test_qq_official_admin_identity.py)
- [IdentityHTTPTests.test_body_cannot_supply_openid_account_or_admin_and_csrf_required](../tests/test_qq_official_admin_identity.py)

覆盖边界：本机 ASGI/PG 流程使用合成 C2C 帧。local_admin 不自动获得 OwnerGrant、官方管理员绑定或审批 quorum；真实双端产品操作仍需受控验收。

## 10. 凭证不外泄

拒绝将真实凭据发往非信任 API/网关，loopback 特例只接受明确 TEST 凭据。OTP 不进入 ledger、计划、回执正文或公开普通 hash；下载 bearer URL 不进入访问/异常日志。App 公共状态不回显凭据，缺原始凭据 bundle 的恢复保持停止。

对应测试：

- [CredentialTests.test_untrusted_endpoints_cannot_receive_credentials](../tests/test_qq_official_http.py)
- [OfficialReceiptTests.test_otp_code_never_enters_receipts_ledger_manifest_or_public_hash](../tests/test_qq_official_receipts.py)
- [LinkTests.test_transport_logs_do_not_contain_url_or_injected_error](../tests/test_qq_official_file_links.py)
- [OfficialInstancesTests.test_actual_revision_save_snapshot_environment_and_public_non_disclosure](../tests/test_desktop_official_instances.py)
- [OfficialRestoreTests.test_missing_original_bundle_blocks_start_and_replacement_credentials_persistently](../tests/test_desktop_official_restore.py)
- [PartTransportTests.test_real_httpcore_debug_response_headers_are_redacted_only_in_upload_context](../tests/test_qq_official_native_session.py)
- [PrivateAclPolicyTests.test_any_effective_allow_for_everyone_users_or_other_user_is_rejected](../tests/test_qq_official_windows_private.py)
- [WindowsPrivateFileTests.test_real_restricted_acl_is_readable_and_broad_grant_is_denied](../tests/test_qq_official_windows_private.py) — **Windows Server 2025 原生 CI 已通过，零跳过；见下文证据。**
- [WindowsPrivateFileTests.test_acl_change_on_open_descriptor_is_checked_again](../tests/test_qq_official_windows_private.py) — **Windows Server 2025 原生 CI 已通过，零跳过；见下文证据。**
- [WindowsLocalVolumePolicyTests.test_same_handle_local_volume_guid_is_required](../tests/test_qq_official_windows_private.py)
- [WindowsPrivateFileTests.test_symlink_is_rejected_when_symlinks_are_available](../tests/test_qq_official_windows_private.py) — **Windows Server 2025 原生 CI 已通过，零跳过；见下文证据。**

覆盖边界：新增分片以真实 httpcore2 DEBUG 响应头作泄漏哨兵，只有上传上下文被脱敏。Windows ACL 纯策略/伪 WinAPI 测试与真实 Windows 文件系统测试分开；后者已有下文所列实际 CI 结果。有限哨兵测试不是全系统形式化保密证明。

## 11. 能力与授权分开

工具定义与实际执行均检查能力，已展示能力撤回后调用仍拒绝。实现支持不等于端点授权，平台群主也不自动获得本地群记忆写权限。文件证据绑定目标、generation/route/capability revision、类型、原 contract/期限；空路线和仅小文件证据不能因宿主支持 multipart 自动升级。

对应测试：

- [PlatformToolGatesTests.test_all_feature_gates_apply_to_definition_and_execution](../tests/test_platform_tool_gates.py)
- [CapabilityEvidenceTests.test_implementation_features_are_not_endpoint_grants](../tests/test_platform_types.py)
- [MemoryServiceTests.test_platform_owner_is_not_local_group_write_authority](../tests/test_qq_official_business_commands.py)
- [FileServiceTests.test_native_evidence_is_exact_and_not_inferred_from_platform_role](../tests/test_qq_official_file_services.py)
- [FileConfigurationTests.test_default_off_empty_routes_allow_protected_pickup_but_no_route_evidence](../tests/test_desktop_official_files.py)
- [NativeChunksTests.test_old_small_only_target_evidence_does_not_upgrade_from_host_configuration](../tests/test_qq_official_native_chunks.py)

覆盖边界：原生大文件仍要求新的、显式 multipart=true 的目标证据；本机成功不能签发真实目标能力。图片 3.5 MB 限制、默认文件额度与各目标原 max_bytes 不因分片实现而放宽。

## 12. @ 和引用不可伪造

普通文字中的平台标签（包括跨 TextNode 拼接）被转义；只有结构化、同账号/环境/群的已许可 ActorKey 能形成原生 @。引用索引冲突保留原映射并标歧义，同字面索引来自另一账号、环境或端点时不能触发引用回合。

对应测试：

- [OutgoingTests.test_platform_tags_are_literal_even_in_markdown](../tests/test_qq_official_http.py)
- [RendererTests.test_tag_split_across_text_nodes_is_escaped](../tests/test_qq_official_renderer.py)
- [RendererTests.test_only_structured_authorized_member_emits_native_mention](../tests/test_qq_official_renderer.py)
- [RendererTests.test_same_literal_member_different_app_env_or_group_not_authority](../tests/test_qq_official_renderer.py)
- [OfficialReceiptTests.test_ref_idx_collision_keeps_original_mapping_and_marks_ambiguity](../tests/test_qq_official_receipts.py)
- [QQOfficialIngestTests.test_same_index_other_account_endpoint_or_environment_cannot_trigger](../tests/test_qq_official_ingest.py)

覆盖边界：这里证明 codec/renderer/索引边界，不证明 QQ 各客户端原生 @、引用或 Markdown 的视觉呈现。实际 bot 身份另由认证 READY/self API 证据管理，不能从 AppID 或请求者推导。

## 13. 单实例有边界

共享 PostgreSQL 中同账号跨 schema 仍竞争同一 advisory lock，不同账号/环境分离。backend 死亡使原 token 失效；发送前失租约不调用 REST。App 仅提示本地同 AppID 风险，明确 global exclusivity 为 unknown。分片 DNS 返回后重新查完整源权限，端点撤销或 job generation 改变时零 PUT并保留 unsent 事实。

对应测试：

- [LeaseTests.test_same_account_in_another_schema_still_competes](../tests/test_qq_official_lease.py)
- [LeaseTests.test_account_and_environment_have_separate_locks](../tests/test_qq_official_lease.py)
- [LeaseTests.test_backend_death_is_rejected_before_takeover_or_probe](../tests/test_qq_official_lease.py)
- [OfficialSenderTests.test_lost_lease_before_handoff_does_not_invoke_rest](../tests/test_qq_official_sender.py)
- [OfficialInstancesTests.test_same_app_other_local_instance_risk_does_not_claim_global_exclusivity](../tests/test_desktop_official_instances.py)
- [NativeChunksTests.test_endpoint_revoked_during_dns_recheck_blocks_first_physical_put](../tests/test_qq_official_native_chunks.py)
- [NativeChunksTests.test_job_lease_loss_during_dns_retains_unsent_historical_evidence](../tests/test_qq_official_native_chunks.py)

覆盖边界：锁只覆盖同一 PostgreSQL 数据库。独立数据库部署必须使用不同 AppID 或明确停旧接新；数据库锁和本地实例扫描都不能证明全网单实例。

## 14. 项目访问须证明

旧项目创建者/editor 或近期发言不能替代官方 ProjectGrant。任务固定原 grant revision，显式重授也不使旧任务引用复活。仅收件人近期证据失效时可在原期限内等待同一 artifact/plan，并在真实新 M2 入站后继续；撤原 grant 或到达原 grant 截止则取消且不上传。

对应测试：

- [ProjectAuthorityTests.test_legacy_project_creator_or_editor_does_not_supply_official_grant](../tests/test_qq_official_project_authority.py)
- [ProjectAuthorityTests.test_old_task_ref_stays_invalid_after_explicit_regrant](../tests/test_qq_official_project_authority.py)
- [ProjectAuthorityTests.test_fresh_evidence_required_for_delivery_but_does_not_grant_read](../tests/test_qq_official_project_authority.py)
- [ProjectAuthorityTests.test_actual_task_binding_freezes_original_grant_revision](../tests/test_qq_official_project_authority.py)
- [ProjectCaptureAuthorityTests.test_wait_does_not_mask_revocation_or_original_grant_expiry](../tests/test_qq_official_project_capture_authority.py)
- [ProjectCaptureAuthorityTests.test_after_301_seconds_same_artifact_and_plan_wait_for_real_inbox_without_renewal](../tests/test_qq_official_project_capture_authority.py)
- [ProjectCaptureAuthorityTests.test_original_deadline_ends_wait_without_upload](../tests/test_qq_official_project_capture_authority.py)

覆盖边界：本地生成与敏感物理交付使用不同检查：允许等待不等于延长授权、重发 unknown 或绕过 source/job/route。测试所用模型与 QQ 为假服务，不自动开启真实宿主高风险操作。

## 15. 任务完成不等于交付完成

内容验收、文件保存/上传、消息被接受和用户下载分别记账。报告从当前文件状态生成，不因传输未知撤销已经完成的内容验收，也不因消息被接受把失败内容改成通过。回执 finalize 幂等推进真实 bot 作者、索引、journal 和 charge；未知响应只计 unknown。

对应测试：

- [OfficialFileReportingTests.test_transport_pending_or_unknown_does_not_undo_content_acceptance](../tests/test_qq_official_file_reporting.py)
- [OfficialFileReportingTests.test_accepted_message_does_not_claim_download_or_pass_failed_content](../tests/test_qq_official_file_reporting.py)
- [OfficialReceiptTests.test_finalization_commits_real_bot_ledger_index_journal_and_charges_once](../tests/test_qq_official_receipts.py)
- [LinkTests.test_actual_get_hash_headers_and_complete_is_not_downloaded](../tests/test_qq_official_file_links.py)
- [SenderMetricsTests.test_actual_lost_response_is_unknown_and_never_counted_as_acceptance](../tests/test_qq_official_metrics.py)

覆盖边界：HTTP 完整流仅增加 served_complete_count，HEAD 不算下载，平台接受也不等于用户下载或打开。既有用户人工确认只覆盖当时的具体文件/客户端，不能推及所有格式。

## 16. 迁移与回退可审计

冻结与发送竞争必须留下实际 dispatch activity；已成功旧消息不转换重发，多个旧结果中 unknown 优先隔离。转换 proof 限同事务且原期限不续；回退推进路由版本、拒绝旧 epoch，并保留已应用的历史回执。

对应测试：

- [JournalTests.test_freeze_and_dispatch_race_has_no_untracked_send](../tests/test_qq_official_migration_journal.py)
- [PreviewTests.test_successful_old_delivery_is_confirmed_and_never_reissued](../tests/test_qq_official_migration_preview.py)
- [PreviewTests.test_two_finals_unknown_dominates_confirmed_independent_of_insert_order](../tests/test_qq_official_migration_preview.py)
- [JournalTests.test_conversion_proof_must_be_from_same_transaction_and_deadline_not_renewed](../tests/test_qq_official_migration_journal.py)
- [JournalTests.test_rollback_revokes_grants_and_old_epochs_while_preserving_receipts](../tests/test_qq_official_migration_journal.py)

覆盖边界：Journal 回退用例的历史撤销标记是夹具端口，不能单独声称所有真实历史消费者都已验证。本项是本地切换/CAS 证据；实际群历史迁移和生产切路由仍需用户审阅确认。

## 17. 文件清理与有效期一致

产物与链接都有固定期限和容量上限；有效 publication 通过共享锁与引用阻止 GC 删除字节，过期后保留审计并清理字节。已结束 producer 不会使已托管文件丢失；旧维护租约不能删文件。重启、探测过期或时钟回退不续签链接、不换 token；真实源撤销仍阻止 GET。

对应测试：

- [IntegrationTests.test_link_expired_then_artifact_gc_keeps_audit_and_no_live_bytes](../tests/test_qq_official_file_links_integration.py)
- [PgPublicationTests.test_expiry_persists_across_clock_rollback_and_restart](../tests/test_qq_official_file_publications.py)
- [FileServiceTests.test_stale_maintenance_cannot_tombstone_or_remove_bytes](../tests/test_qq_official_file_services.py)
- [SubAgentFileAuthorityTests.test_completed_subagent_file_survives_producer_completion_and_lease_expiry](../tests/test_qq_official_file_authority.py)
- [DownloadTests.test_actual_get_head_range_no_session_and_original_expiry](../tests/test_qq_official_download_runtime.py)
- [DownloadTests.test_probe_expiry_does_not_reissue_publication_and_epoch_change_restores_same_token](../tests/test_qq_official_download_runtime.py)
- [DownloadCompositionTests.test_actual_protected_factory_m3_receipt_head_get_and_current_source_revocation](../tests/test_qq_official_download_composition.py)
- [NativeChunksTests.test_final_file_info_restores_after_prepare_expired_and_private_session_collected](../tests/test_qq_official_native_chunks.py)
- [NativeChunksTests.test_session_requires_exact_prepare_receipt_and_is_immutable](../tests/test_qq_official_native_chunks.py)
- [NativeChunksTests.test_real_protected_factory_tls_multipart_merge_then_one_m3_send](../tests/test_qq_official_native_chunks.py)
- [ArtifactTests.test_artifact_quota_and_nonrenewing_deadline](../tests/test_qq_official_file_artifacts.py)
- [PgPublicationTests.test_publication_share_lock_blocks_gc_then_reference_pins_bytes](../tests/test_qq_official_file_publications.py)
- [ArtifactTests.test_artifact_immutable_manifest_and_monotonic_retention](../tests/test_qq_official_file_artifacts.py)

覆盖边界：分片 session 是原 prepare attempt/receipt 的不可变记录；私密 prepare 文件过期清理后，已有最终 file_info 可恢复到 M3，不能为恢复重开会话。最长保留与单调性通过数据库断言覆盖；真实公网反代、腾讯域名条件和收件人可达性仍需分别验收。

## Windows、原生分片与真实平台验收

`WindowsPrivateFileTests` 由 `os.name == "nt"` 控制。在 macOS/Linux 上跳过不能记为 Windows 通过；符号链接用例还可能因 runner 没有创建链接的权限单独跳过。`WindowsLocalVolumePolicyTests` 只模拟同句柄 Volume GUID 查询，不能替代真实 NTFS/DACL 测试。

实际 Windows 证据：PR #9 的提交 `5f58535f2dcdb7dd4daea746b208756973998884` 在 Windows Server 2025 / CPython 3.12.10 上运行 `tests.test_qq_official_windows_private`，13 项测试全部通过，耗时 0.164 秒，零跳过；三个原生用例（受限 ACL 可读、打开描述符上的 ACL 修改复验、符号链接拒绝）均为 `ok`。[Windows credential ACL — job 123707319059](https://github.com/zty20040403/gaojibot/actions/runs/40947626685/job/123707319059)。测试夹具先重置遗留显式 ACL 再授实际测试用户权限，产品 ACL 策略未放宽。

该结果只关闭上述原生文件 ACL 检查，不等于 Windows CLI 的受保护报告创建、报告目录 ACL 和完整探测流程已获真机验收；这些边界仍需单独记录实际证据。

原生分片自动化已包含真实本机 TLS PUT、持久 prepare/part/complete、未知结果、撤权、租约变化和恢复。它没有使用真实 QQ 凭据，也不证明平台已为目标开放大文件、签名上传主机白名单正确、真实客户端可以下载。旧小文件证据必须保持原边界；真实大文件验收按[方案第 11 节](qq-official-plan.md#11-用户真机验收清单执行者整理好交给用户)受控进行。

本机 download 探测只能证明配置的 HTTPS 入口返回同一后端的随机挑战；它不能代替 QQ 域名条件或收件人可达性。已有人工验收应保留版本、环境、目标范围、时间与脱敏证据，不能从一次 PDF/ZIP 下载推广出所有客户端、格式或故障场景的结论。
