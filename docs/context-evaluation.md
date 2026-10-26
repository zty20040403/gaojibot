# 上下文评测

评测分为两层，不能混用它们的结果：

1. `tests/test_context_accuracy_eval.py` 检查连续原文是否进入上下文、检索结果是否经过范围过滤。
   其中语义结果是测试替身，不代表真实 BGE-M3 检索质量。
2. `tools/context_eval.py` 对实际生成的回答评分：关联对象、证据编号、正文必要信息、
   不相关内容、个人事实归属、跨群内容，以及真实返回的前五条检索结果。

评测不会改变线上上下文策略，不会截断成固定条数，也不会增加追问澄清阈值。

## 不调用模型：评分已有结果

```sh
python tools/context_eval.py --predictions /tmp/context-answers.json --output /tmp/context-report.json
```

输入是 JSON 数组，每条包含 `case_id`（场景名加冒号和变体编号，从 0 开始）、
`answer`、`focus_id`、`evidence_ids`、`scope_key`。
需要检索的场景还要提供 `retrieved_ids`；涉及个人事实时提供
`personal_facts: [{"user_id": 7, "evidence_id": 1}]`。
关联编号使用场景里的原始 `id`，不是数据库自增编号。

缺失、报错和超时的样例也计入分母，不会因为失败被剔除而得到虚高分数。
重复或未知的 case_id 会直接报错。退出码 0 表示通过，1 表示未通过。

## 显式调用真实模型和 BGE-M3

先在环境变量中提供已有模型配置与 embedding 配置，不要将 Key 写进命令或提交到仓库。
运行一个受限样本批次：

```sh
python tools/context_eval.py --live --profile qwen-local --limit 5 --embeddings --output /tmp/context-live.json
```

`--limit` 是场景数；一个场景可能包含多个追问变体。`--live` 才会产生模型调用费用。
`--embeddings` 使用配置中的 embedding 模型，当前项目配置为 BGE-M3。
向量由实际模型生成，再在隔离样本内用精确余弦相似度排序，不读取预设 `semantic_order`。
它测试语义模型，不测试生产 pgvector 索引性能。省略该开关时不能声称检索达标。

使用生产的 Ledger/ContextStore 构造连续时间线，并通过现有模型网关生成回答。
模型看不到 `expected_focus`、`required`、`forbidden` 等答案标签。
运行数据保存在临时目录与内存数据库中，不写入线上聊天记录，不发送 QQ 消息。
默认最多 5 个场景。完整批次可以把 limit 增大到场景总数。

## 如何读报告

- `fixture_count` 是不同场景数，`prompt_count` 是包含变体的样本数，不能把变体说成独立真实案例。
- `focus_accuracy` 是关联对象匹配率；`recall_at_five` 是期望原文出现在检索前五条的比例。
- `strict_answer_pass_rate` 是规则验收通过率，不等同于人工判断的整体聪明程度。
- `cross_scope_count`、`wrong_personal_fact_count` 分别记录串群与结构化个人事实归属错误。
- `cases[].reasons` 给出每个样例失败原因，原始生成内容保留在 `predictions` 中方便人工核验。

当前验收门槛：关联匹配和规则通过率至少 90%；存在检索场景时 Recall@5 至少 90%；
串群和个人事实归属错误为 0；不相关正文低于 5%。这些是目标，不是已测得的成绩。

关键词规则可能误伤合理改写，也可能漏掉隐含的张冠李戴。因此上线前还应人工抽查
失败案例和一部分通过案例。这个隔离基准不是完整 QQ 工具调用、沙盒或多轮对话的端到端评测。

本轮改动只运行离线评分和回归测试，未花费 API Token 跑完整模型基准。

## 真实回答的表情反馈

`AI_FEEDBACK_POSITIVE_EMOJI_IDS`、`AI_FEEDBACK_NEGATIVE_EMOJI_IDS` 是逗号分隔的
NapCat emoji ID。两项默认留空，不采集投票。先在自己的测试群核实实际通知中的
`likes[].emoji_id`，再配置，不能直接把测试里的编号当成生产默认值。
通知结构参考 [NapCat 事件定义](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/event/notice/OB11MsgEmojiLikeEvent.ts)。

- 只处理启用群里、已经关联到回合的机器人消息。忽略机器人自己、其他人的消息和未知用户通知。
- 每个回合每人一票；改贴表情更新同一票，撤销当前表情撤票。`likes[].count` 不当作人数。
- 重复或时间更旧的通知不改变结果；同秒同表情撤销优先，避免延迟重发又恢复票数。
- 提问者独立显示，其他人的票分别计数。控制台“有差评”包含任一负票或管理员的“答非所问”。
- 管理员判断不会被群友覆盖。导出时优先用管理员标签，其次提问者，最后群友多数票；平票不生成标签。
- 采集不调用模型、不回复群消息。SSE 会更新列表和详情，正在填写的评价保留。

## 私密导出与 A/B 回放

先由管理员明确提供 PostgreSQL 连接环境变量 `AI_POSTGRES_DSN` 和
`AI_POSTGRES_SCHEMA`，工具不会读取 `.env`、启动机器人或执行迁移。
以下 `GROUP_ID` 换成获准评测的群号：

```sh
python tools/export_feedback_cases.py --scope onebot-v11:group:GROUP_ID \
  --limit 100 --history-limit 500 --output feedback-exports/cases.json
```

导出在单个 PostgreSQL 只读、一致性快照中进行。包括问题、证据编号、此前可见原文、
原回答、标签、票数、模型和内容哈希。不会读取其他群、隐藏消息或问题之后的消息。
`has_more` 表示仍有更早回合未导出。这里的 500 条是离线导出上限，不改变线上连续时间线。
文件权限为 0600，不覆盖已有文件；目录已被 Git 忽略。**正文仍然是私密群聊，不是匿名数据。**

回放前审核文件内容，确认允许发送给选定的两个模型供应商，再执行：

```sh
python tools/context_eval.py --replay-export feedback-exports/cases.json \
  --profile CANDIDATE_PROFILE --judge-profile JUDGE_PROFILE \
  --allow-provider-data --limit 5 --timeout 120 \
  --output feedback-exports/comparison.json
```

候选模型通过现有 Ledger、ContextStore 和提示词构造器重建连续原文时间线，
不能看到原回答、标签或被选中的证据答案。评委看到随机化位置的 A/B 回答与相同历史；
不会告诉它哪个是新版，也不提供标签。两个调用分别限制为明确选择的 profile，
不允许悄悄降级到第三个供应商。只有显式回放命令产生 API 消耗。

报告分开统计负样本的 win/tie/loss 和正样本退步数量，同时显示 error、skipped、coverage、
实际模型和 Token。空回答、超时、非法评判不是平局；未比较案例不会伪装成提升。
单案例哈希用于发现文件改动，不是签名或防篡改认证。退出码 0 只表示有比较结果且无调用错误，
不代表质量达标；`release_ready` 始终为 false，需人工抽查。

当前边界：只重放文字上下文，不执行历史工具命令、不发 QQ、不重新下载媒体。
工具回合、多媒体、证据不在快照内，以及原回答可能被日志长度限制截断的案例会明确跳过。
不会拿今天的个人记忆或 Historian 章节假装过去的快照。因此这不是完整生产回放，
真实 QQ 贴表情、真实供应商 A/B 和长期记忆回放的验收仍需分别进行。
