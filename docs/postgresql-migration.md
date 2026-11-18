# PostgreSQL schema 与历史离线导入

## 当前支持范围

Bot 宿主只支持 PostgreSQL + pgvector；SQLite 不是运行时后备库。运行时不建表、
不改表，数据库不可达或 revision 不匹配时拒绝启动。当前入口仍是：

```sh
gaoji-db upgrade
gaoji-db current
gaoji-db check
```

命令读取显式提供的 `AI_POSTGRES_DSN` 和 `AI_POSTGRES_SCHEMA`。NixOS module
启动前执行 upgrade 或 check；本手册不描述任何现存生产拓扑或备份定时器。

## 0114：只退役两个空表

`0114_retire_legacy_import` 在同一迁移事务中先取得 `maintenance_state` 和
`embeddings` 的 `ACCESS EXCLUSIVE` 锁，再检查两表。任何一张非空都会报错，
revision 和全部表/数据保持原样。通过后逐个显式 `DROP TABLE ... RESTRICT`，
不使用 CASCADE；额外视图/外键依赖也会令事务失败。不要为通过 upgrade 自动清表。

`storage_migration_runs`、`message_topic_edges`、`delivery_attempt_quotas` 不变。
当前向量读写走 `semantic_documents`，不是这里退役的旧 `embeddings`（bytea）。
降级到 `0113_group_attention_scheduler` 重建两个空表及原约束、索引、identity；
不恢复外部自定义对象或历史数据。这里没有删除非空表的授权路径。

## SQLite/JSON 用户：保留旧版离线入口

当前包不再携带 `legacy_migration.py` 或 `inspect-legacy`、`migrate-legacy`、
`verify-legacy`。它们保存在已发布历史提交
[`23f452c9c970956207a5b6d46022cddf6731eb41`](https://github.com/zty20040403/gaojibot/tree/23f452c9c970956207a5b6d46022cddf6731eb41)
（schema head 为 0113），不是把旧导入器直接拿来写新版 schema。

先停止所有旧/新 Bot 写入者，完整备份旧目录；如果已有 PG 数据，也先做完整
`pg_dump` 并核实备份可读。保留原目录、导入报告及迁移前 PG 备份，不覆盖现有库。
旧版会忽略它不认识的文件/表，因此必须先盘点源数据，对照该提交
`src/bot_storage/legacy_migration.py` 的 `TABLES` / `JSON_STATES` 逐项核对。
未知文件、额外列、已退役功能的数据不在无损承诺内：必须停止，另行核对/导出，
不能仅凭 `verified=true` 宣称整个旧目录都已保全。

以下在本地完整 Git 历史可用、当前冻结依赖已安装的条件下执行。`AI_POSTGRES_DSN`
必须由操作者指向**新建的空 PG 数据库**，`AI_POSTGRES_SCHEMA` 指向该库中的导入
schema；`STATE_DIR` 是已备份、停写的旧目录的绝对路径。不要读取生产配置来填这些变量。

```sh
# 从当前源码根目录开始；不切换当前 checkout，不启动历史版 Bot。
set -eu
: "${AI_POSTGRES_DSN:?显式指定新空库}" "${AI_POSTGRES_SCHEMA:?显式指定导入 schema}" "${STATE_DIR:?指定已备份的旧目录}"
GAOJI_PYTHON="$(uv run --frozen --python 3.13 python -c 'import sys; print(sys.executable)')"
LEGACY_ROOT="$(mktemp -d)"
git archive 23f452c9c970956207a5b6d46022cddf6731eb41 | tar -x -C "$LEGACY_ROOT"
(
  cd "$LEGACY_ROOT"
  "$GAOJI_PYTHON" -m src.bot_storage.cli upgrade
  "$GAOJI_PYTHON" -m src.bot_storage.cli inspect-legacy --state-dir "$STATE_DIR" --report inspect.json
  # 先人工核对 inspect.json；不带 --apply 的 migrate-legacy 也是 dry-run。
  "$GAOJI_PYTHON" -m src.bot_storage.cli migrate-legacy --state-dir "$STATE_DIR" --apply --report imported.json
  "$GAOJI_PYTHON" -m src.bot_storage.cli verify-legacy --state-dir "$STATE_DIR" --report verified.json
)
# 确认上述每步 exit 0，且所有报告 verified=true（inspect 只提供盘点）。
# 回到当前源码，仍对同一新库/schema 操作：
"$GAOJI_PYTHON" -m src.bot_storage.cli upgrade
"$GAOJI_PYTHON" -m src.bot_storage.cli check
```

旧版先创建它自己的 0113 schema，再原子导入、重置 identity、逐表摘要 verify。
任一步失败都停止；不要自动加 `--resume` 覆盖已有数据。若 0114 检出非空候选表，
保留新库在 0113 和所有源文件/报告/备份，由数据所有者单独决定归档方案；这条
流程不保证任意旧目录能自动升至新版。更早版本到 0113 的历史迁移也可能删改旧
功能数据，不能把本次验证当成所有历史升级路径的数据安全证明。

## 已验证范围与限制

本切片测试使用本机一次性 PostgreSQL 17 + pgvector、当前冻结 Python 依赖和
上述固定旧提交：全新安装、旧 CLI 的 SQLite conversation/JSON 导入与 verify
后实际执行新版 upgrade/check、源文件不变、主键续增、导入审计保留；另测两个
候选表各自非空拒绝、未提交写者阻塞迁移、额外视图依赖拒绝和原事务回滚、空表 downgrade 的完整
列类型/默认值/约束/索引/identity 对照。原快照摘要、非法 JSON、语义 key 规范化
测试继续对固定历史实现运行。它们不覆盖任意用户数据、完整历史功能或生产环境。

仅验证小写 schema；额外大写 schema 尝试在既有 0024 的未引用标识符处失败，未修改历史迁移。
备份恢复必须在操作者自有隔离库实际演练；本次未连接生产、读取凭据、部署或做
真实用户数据导入。切换后如已有新消息，不能恢复旧 SQLite 目录后继续运行并
宣称无损；先停写、保留两侧数据，再人工核对增量，禁止两版同时处理消息。
