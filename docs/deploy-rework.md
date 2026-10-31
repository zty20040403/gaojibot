# 改造上线步骤

本文对应 W1–W7、W9–W10 的统一上线；W8 GitHub 暂缓。生产库的改动在部署时手工执行，执行前先备份。

## 1. 部署前

1. 备份数据库（用 PostgreSQL 17 的 `pg_dump`，tank 默认的是 16 版）。
2. NixOS 配置里停掉 NapCat：`gaoji-napcat.service` 不再需要，bot 不再读取 `AI_ONEBOT_ENABLED`。
3. `AI_SYSTEM_PROMPT` 继续作为人设。宿主配置移除旧主动接话 percent/threshold/max_checks_per_hour，核对媒体和文件生成已启用。
4. Nix 配置删除 oneBot、napcat、browser、codesnap、cluster 和 videoDeep 选项；宿主只读运维改用 opsRead，短语音本地识别改用 voiceAsr。cluster-control 服务本身保留。
5. 已有 VM 显式执行 sandbox_tools_setup.upgrade，新模板预装浏览器、webread、ffmpeg 和 Whisper。升级前保存配置，保持 /work 文件。
6. 先完成全套测试、前端构建和 Nix 构建，再一次性停止旧服务、备份、迁移并启动新版；保持原 PostgreSQL 主备关系。

## 2. 部署

服务启动前 `gaoji-db upgrade` 执行到 `0077`：新增窗口、记忆、请求、后台任务和定时任务表，删除 104 张废弃功能表。仍在使用的 Fleet 表保留供 cluster-control 使用。先使用 PostgreSQL 17 的 pg_dump 保存完整备份，并验证归档可读。

## 3. 部署后的数据调整

下面的 `<GROUP>` 换成官方群绑定的旧群号，`<OLD_CONV>` 和 `<BOUND_CONV>` 换成会话编号：前者是绑定前官方群自己的会话，后者是绑定后使用的旧群会话。执行前先用 SELECT 核对。

```sql
BEGIN;
-- 绑定前官方群自己的聊天记录并入绑定后的会话；编号顺序本来就接得上。
UPDATE messages SET conversation_id = <BOUND_CONV> WHERE conversation_id = <OLD_CONV>;
DELETE FROM conversation_visibility WHERE conversation_id = <OLD_CONV>;
DELETE FROM conversations WHERE conversation_id = <OLD_CONV>;

-- 窗口从官方群开始的地方起算，更早的记录用 history 查。
INSERT INTO conversation_windows (scope_key, tail_after, updated_at)
VALUES ('onebot-v11:group:<GROUP>',
        (SELECT min(canonical_message_id) - 1 FROM messages WHERE conversation_id = <BOUND_CONV>
           AND occurred_at >= extract(epoch FROM timestamp with time zone '2026-10-30 00:00+08')),
        extract(epoch FROM now())::bigint);

-- 旧的长期记忆整理成这个群的初始记忆文件，之后由夜间整理归类。
INSERT INTO conversation_memory (scope_key, content, updated_at)
SELECT 'onebot-v11:group:<GROUP>',
       string_agg('- ' || (e->>'content'), E'\n' ORDER BY (e->>'id')::int) || E'\n',
       extract(epoch FROM now())::bigint
FROM state_blobs, jsonb_array_elements(payload_json::jsonb->'entries') e
WHERE namespace = 'long_term_memory'
  AND (e->>'scope_key' = 'group:<GROUP>' OR e->>'scope_key' LIKE 'group:<GROUP>:user:%');
COMMIT;
```

## 4. 验收

- 在群里 @bot 问「刚才聊了什么」：只说 10-30 之后的内容。
- 问「本群最早的聊天记录是什么」：调用 `history`，答出最早一条。
- 「发个表情」：发出一张图片，不出现「[酷]」。
- 说「以后叫我 X」，再问「我叫什么」：能答对；`/memory` 能看到这一条。
- `/clear` 之后再问「刚才聊了什么」：没有内容；`history` 仍能查到之前的记录。
- 别人之间的闲聊不插嘴；被要求闭嘴时不回复。
- 出了问题时，在 `turn_requests` 里按 turn_id 查当时发给模型的完整请求。

## 5. 回滚

`0077` 会删除旧功能表，不能只切旧代码或调用 downgrade。回滚须停服务，恢复切换前的数据库备份、宿主配置和对应 Nix generation；第 3 步的数据调整也通过该备份恢复。
