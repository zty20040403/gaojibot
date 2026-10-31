# 改造上线步骤

本文对应 `rework/p0` 分支：删除 OneBot 和不用的功能，接入新的提示词、聊天窗口、记忆文件和 `history` 工具。生产库的改动在部署时手工执行，执行前先备份。

## 1. 部署前

1. 备份数据库（用 PostgreSQL 17 的 `pg_dump`，tank 默认的是 16 版）。
2. NixOS 配置里停掉 NapCat：`gaoji-napcat.service` 不再需要，bot 不再读取 `AI_ONEBOT_ENABLED`。
3. 不需要改环境变量。`AI_SYSTEM_PROMPT` 照旧作为人设；没设时使用 `prompt.py` 里的默认人设。

## 2. 部署

服务启动前 `gaoji-db upgrade` 会执行迁移 `0073`，新增三张表：`conversation_windows`、`conversation_memory`、`turn_requests`。

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

回到 tag `pre-rework-20261031` 并重新部署；`0073` 新增的表不影响旧代码。第 3 步的数据调整需要用备份恢复。
