# F8 双边连接与跨会话问答

状态（2026-11-15）：连接控制与私有question已接真实账号factory/monitor/recover/close，`/link` 本地可用；问答schema/来源/reducer/worker与受限目标模型本地已实现，`ask`源入口已接，管理审计只读API/UI已验，未真实QQ验收或合并/部署。账号问答组件23059ae见Draft PR #112（等价a863385）。权威要求见 `next-features-plan.md` F8，参考 maxx 固定设计的 `Asking another conversation`。

## 连接不是身份映射

- 单向 A→B 只允许 A 的 bot 向 B 的 bot 提问；没有反向权利。连接面向实际物理 endpoint，不是 canonical 历史、姓名、人员编号或跨群 openid 的推测。
- 仅同一官方账号（完整 AccountKey，包括 scope）的已准入会话；不提供跨宿主账号访问。源/目标均需当前路由与成员状态有效。历史别名不是额外授权，也不把路由改为对端历史。
- `/link` 在 A 由当前宿主 owner 或原认证群主/群管申请；私聊不把普通 peer 当作全局 owner。B 的 owner/原认证群管在 B 输入 `/link <code>` 才确认 A→B。相同 endpoint 不能兑换；重复确认不重复建立，反向要另做一次。
- 普通成员不能因知道验证码就建立连接；引用、转发、模型文本、网页、文件、VM 请求与裸 role 字段都不是认证命令。禁止自动按成员关系建立边。
- LinkManagement的list/status/cancel/revoke已接真实账号原认证TxA，仅原认证owner/群管读写自己物理端点涉及的同账号记录，查找后复查原600秒期限。list在LIMIT前限定范围，最多32条及more；只返回散列会话标签、方向/状态/版本，不泄露其他账号/会话的openid。控制反馈与真实账号工厂已接，本地可验而非QQ实测。
- 默认owner私聊不依赖/bind历史绑定：新private/unbound/present且canonical scope为原物理私聊时可核验owner grant/准入；不放开unbound群、普通public peer、revoked或unbound外部历史。一次link控制在主动状态尚未明确allowed时只选择原认证msg_id窗口，不先探测失败再重试；已有明确allowed仍遵守一次尝试。默认store入口及真实账号factory已用loopback验，本地结果不代表QQ权限。
- 原认证 `/link revoke <link-id>` 提供明确方向；任一端可撤销涉及自己的授权，不能删除无关边。现有链接保留独立版本，重建不能复活旧问答。当前管理审计页只读，不提供管理台撤销或独立 `/unlink` 的替代授权通道。

## 独立短码与持久来源

- 复用 `/bind` 的交互形式，不复用 binding challenge、历史重绑定或 owner grant。六位码最多600秒，只存专用 HMAC；原命令收到/批准时间不被等待、重试或后台执行延长。
- 只有原认证消息能发起/确认。原账号 epoch、source endpoint/route generation、actor/权限凭证与原入站回执固定；源权限变化、原租约失效或重启取消旧挑战。
- 发行、送达确认、兑换分开：发送不明或重启不自动重发/猜码；快速兑换可持久保存确认，但未核实发行投递的真实成功不得激活连接。
- 新挑战的最终双边current/期限检查必须在已有边持久同意查找之后；旧查找跨期限时不能误报already_linked，更不能用先前时间激活新边。到期保留发行事实，仅关闭新挑战；旧边不因新请求过期而撤销。
- 纯LinkReducer只实现账号锁中的create/confirm/一次HMAC/双边证明/发行状态转换：持久独立限流、最多20个live挑战及8次随机避碰，确保有界查询包含所有可能验证码。快速确认未核实发行不能激活，最终current之后再次核实期限且以同一受检时间写激活。原认证TxA与LinkNotices发送/实际账本取证已接，定向取消/撤边/查询已接可选TxA，控制回复与真实账号工厂已接，只证明本地连接控制，不代表已部署或QQ实测。
- 0100把真实发行回执与授权终态分开：迟到committed可在取消/过期后一次补记HMAC/原宿主收到时间，但不能恢复码/目标/授权。新增激活/连接要求回执证据；旧无证据active只保留审计，不猜成功、不授予新的访问。LinkNotices只从实际发送账本的applied response回执保存发行事实；组件已用loopback FakePlatform验收，真实QQ仍待验收。
- 0103独立link_feedback单次ephemeral计划schema：每0102原结果最多一plan，复合FK固定完整账号/物理端点/requester；原scope/route/generation与期限匹配且排队不超30秒，内容/来源不可改删，已有计划须导出才能降级。LinkFeedback现已接实际enqueue/claim/HTTP前current与sender，只用冻结安全结果及原native窗口，RAM正文须精确匹配；401/429/主动拒绝与底层retry/plain不重放，实际回执保存command空正文。结果超过2000字时明确哪些未显示；账号工厂已接，只有实际accepted回执才称发送，排队与本地替身不代表QQ验收。
- 0102独立每inbox一条控制结果/来源快照：不保存原文或验证码，绑定原actor/回执/epoch/route/原600秒与结果HMAC，不可更新/删除。当前原权限/准入及真实gateway control回执在读取和末尾期限检查中复核；list只用授权散列标签，无权限只准空结果的通用拒绝，不降格泄露。单次ephemeral反馈及实际账号工厂已接；不能把存储证明当作发出回复。
- 0101独立link_otp原source_inbox_id/notice_plan_id：5字段FK绑定原入站账号/物理端点/actor/HMAC回执，原native/event/platform/收到时间不可换；单挑战至多一个原端点/requester发行plan。已接LinkNotices原码/原来源验证、enqueue/claim/sender/TxA及真实账号工厂；旧来源NULL不补猜，已有新审计禁止无导出回退。
- 验证码与输入参数不进入 raw inbox、canonical history、LLM、普通发送账本或审计明文。独立 link namespace 与敏感隔离已接入归一化/TxA/普通router/旧误分类TxB；服务未接齐时明确 `link_control_unavailable`，不落回普通消息。提及伪造或嵌入敏感命令仅隔离，不授予权限。
- 所有状态在 account 锁下提交；DB 用复合 FK 拦跨账号，两个认证确认固定来源。迁移不补猜旧链接；已有连接/挑战审计阻止无导出降级。
- OfficialInboxStore.link_ingress默认None，显式挂载才用原TxA取proof/reduce/capture；普通认证消息也观察撤权，checkpoint/角色/挑战/原入站同事务。LinkIngress.source只读原source_inbox_id并验证实际gateway控制回执、HMAC与末current，不借后来消息。Sender只用冻结原msg_id，后来的合法消息不能提供新窗口；claim后HTTP前复查来源，401/429/主动拒绝/格式失败不重试或降级重发，底层fail亦拦retry/plain。真实account工厂已挂载；默认store仍None可拒绝，缺sender时不授新权，loopback测试不是QQ验收。
- 0099/LinkAuthority已提供无正文的顶层认证角色证据：角色/路由变化推进代数，同秒矛盾撤销、迟到旧角色不升权；owner用原grant/revision，返回前再核600秒截止。这仅反映已观察的角色，不是实时上游角色查询，不能立即发现未收到证据的QQ撤权。原认证发起与确认、固定短期挑战仍不可省；原TxA/一次发送/管理/控制回复及实际account工厂已接。

- LinkRuntime在gateway.run前挂原认证observer与发送authority，先recover旧RAM码再sender.recover，使旧captured未applied回执仍保存真实fact但不激活码。每账号独立最多32控制任务，busy立即关闭未发行挑战；每5秒查20+1 live，异常超量拒绝，失权/到期关闭。退出先drain sender再清组件，未消费已发行码关闭，active持久同意与真实回执不误撤或丢失。
- 发行排队最多30秒；队列到期、丢RAM与重启关闭原码，unknown不重发。已accepted的迟到或双取消回执必须排空到账本且只保存事实，不能将已取消/过期挑战重新授权。显式link管理命令在聊天停止时仍可用但不清stop，问答/自动动作另外核验停止。

## 问答的权限与生命周期

- 验证码/管理命令的600秒期限与已成边的持久同意分开。LinkManagement.current仅对active且有真实committed证据的边验证双方冻结的权限凭证和物理路由；已消费码不会因重启而恢复，但正常已成边可跨lease epoch保留。已观察的撤权/重授、成员/路由/密钥变化或旧NULL回执使旧同意失效，不向远端继承原actor身份。新双边证明必须建新边，不能借旧ID复活等待请求。

- 0104私有question任务schema已建立，复用durable_jobs且队列只保存CQ-ID；完整source execution/account/endpoint/actor与定向link复合FK，原epoch/route/scope/revision/deadline及双方stop/目标budget代数冻结。等待最长7200秒且不超原任务；回答/拒绝或取消/过期/unknown终态及一次consume不可恢复/删除。它只证明结构，不自动创建/读取问答或增加QQ效果。
- QuestionAuthority纯来源读口已接：只接受原宿主ExecutionBinding且匹配实际executing/chat完整来源；普通提问actor不需要或继承双方建边管理员/owner权限。原任务/link/问题的intent HMAC在不同重试时间稳定，current必须读取实际不可变任务行，不能由调用方修正旧stop/budget/时间/终态。双端同意/物理路由/准入、双端stop、目标预算及末尾期限复查；target_input自身current后只给问题和散列标签。读口通过不是已运行问答或已完成taint模型。
- Questions纯持久reducer已接submit/去重/claim/current/回答拒绝/一次consume/关闭恢复，账号锁下live限额128/目标16/原任务4；队列只保存CQ-ID，不复制问答到普通表或result。实际job第一代/一次attempt、owner/lease/kind/payload/物理hash lane持续核验，defer/requeue不重放；关闭/终态queue同事务，过期规范expired，在途未知/restart不猜回答，已完成事实不改。消费必须原来源binding与最终current/期限。实际worker、模型取消与费用收尾已挂账号工厂，ask源入口及只读管理UI已接，不把排队当目标已收到。
- `ask` 提交有限长度的问题，建立独立等待任务；源模型在原任务期限内等待并取得结果和任务编号，最长两小时但不延长原source current。QuestionAsks等待读口028082d/Draft#113（等价e85342e）已实现，仅原ExecutionBinding能poll/close/一次consume；重复取消也保留CQ-ID并关闭队列。领取后的runtime/双端/预算/clock失效拒绝返回正文但保留真实领取事实，consumed_at不是送达。前台catalog/factory已通过本地双端真实工厂回归，实际Source入口与日志隔离已组合验收；只读审计UI已接，新本机固定全套和真实QQ/模型验收另续。无连接和不存在的目标统一拒绝，不枚举他人会话。
- B 只收到问题及安全的来源会话标签，不收到 A 的历史、记忆、文件、原 actor 的 owner/群管授权或被动回复窗口。B 的模型/历史/账单归 B 的物理会话；跨会话来源须独立 taint，不能冒充真人或常驻程序。
- 目标按自身规则回答或拒绝；来源及问题作为 untrusted 资料进入易变上下文。不得挂载需要原 owner 真人私聊的 ops、浏览器个人授权、文件/VM 权限等工具。连接不放开 VM 的跨会话接口，也不复制另一台 `/work`。
- 问答及模型内部通知只在等待任务/回合审计中保存，不是公共聊天消息，不进入 `history` / VM events。目标普通发言仍须显式工具动作与自身主动消息权限/额度/停止门槛；不能把问题、回答或“已收到”自动作为 QQ 通知。
- 原等待任务、链接版本、双端物理路由、双端停止代数、目标自动动作预算代数及账号 epoch 在排队、模型等待、返回和通知边界复查。`/stop`、取消、撤链接、过期或重启关闭旧问答；恢复仅接受新请求，不重放已读结果/未知效果。
- 管理审计API只向登录管理员按完整宿主账号/物理端点列出单向链接和问答元数据；详情需显式账号与CQ-ID，正文不进列表/SSE/普通审计。读取是只读事务，不消费/关闭/授权/执行模型或QQ；active只是存储状态，current授权仍由实际动作复查。连接按created_at/ID实时seek，问答按job-ID上界排除后来的新ID但状态实时；consumed_at不表示结果已返回或QQ送达。UI已接“会话问答审计”，换账号/范围/分页即清正文，旧或忽略abort的请求不回填，权限失效/卸载也清私有状态；只显式打开正文并按文本转义显示，无自动QQ或代批。8个UI脚本/构建及三独立CF验证通过；不提供代替QQ双边确认的通道，未来写入仍须CSRF、独立资源版本与事务审计。

## 目标worker契约（先固定输入，再挂模型）

- 目标执行器只收到本端物理endpoint、冻结本端history scope、CQ-ID、问题、散列来源标签和原current回调；没有源actor、真人消息、窗口、owner/群管、VM或文件能力。问题与标签由quote_evidence转义为conversation_question资料；固定提示要求只用目标会话可见内容，可拒绝隐私，不自动发QQ、不把普通文本当作提交回答。
- QuestionModel组件仅开放目标本端history及绑定当前CQ-ID的ask回答/拒绝，无递归/ops/浏览器/agent/VM/文件/任意模型配置；物理模型偏好、本端冻结scope/tail/memory及每请求current/ModelBudget/真实费用已接。QuestionToolSource独立只读且无actor，支持本端F10 embedding；工具先保留候选，普通文本拒绝，清除继承的普通request_sink，取消也排空quota，最终仍由worker current后存私有答复。组件108项组合/五CF通过，后续账号工厂已接且125项组合/六CF通过，均不代表真实模型验收。
- worker复用账号question队列，并发2/每目标1；模型等待期间持续复查双端/来源/job/期限，取消先排空同一模型等待与真实账单，再原子关闭question/queue。真实用量以conversation_question及目标物理会话计费，actor=None，冻结CQ-ID/队列job来源但不复制问题正文或源身份；费用任务维度单列问答、不冒充后台agent/程序。

- QuestionRuntime在原LinkIngress可用后、gateway.run前恢复旧请求再挂目标worker，即使当前没有AI也关闭旧epoch授权；每5秒128+1 live复查并异常超量拒绝。模型或同库账单不可用时明确拒新question。退出先排空socket/真实费用，再关闭待处理question/queue，保留已完成事实/连接；current在领域DB检查前后复查原runtime token，不能因SQL epoch在清理期间仍有效而让SDK取消后返回工具调用继续请求。群聊用本物理群偏好、私聊用宿主默认模型，不把私聊传进group-only偏好读口或借源真人配置。

- Source PrivateQuestionAudit隔离组件2e2c931/Draft#114（等价5411fb4）由内核在同批模型说明前获得ask标记；后续普通request记录关闭，loop事件（含其他工具的被污染参数/结果/fingerprint）仅安全元数据，source trace只留真实usage/模型元数据，不保存私有正文。非ask回合继续原日志；私有工具后模型失败也执行隔离。正常本端用户回复仍按原权限，不自动QQ转发问答。3个实际Source factory/PG/Gateway回归只注入工具验证隔离，随后Source ask catalog/factory已另接并验收。

- 共享guarded_wait的current失效可能先进入模型取消清理，外层账号取消随后到达；清理必须shield/drain同一模型task而非直接await，防止二次取消打断真实socket/usage收尾、提前关闭question/queue。真实factory确定性退出回归保持stopping未完成、question仍running、原requests=1及真实usage断言。

- 前台ask组件3965080/Draft117（等价c9b86c9）按target/question调用原QuestionAsks；只能由原OfficialTurnPlatform/token/ExecutionBinding的chat/final入口建立session，source/endpoints/actor/scope/route/native全部匹配。宿主先范围后LIMIT33读取本物理source当前双边同意的可用方向，给不透明连接ID/散列标签供模型选取；不按群名、历史别名、openid猜目标，反向/停止/预算不可用目标不列出，旧目录不授权新请求。后台/VM/目标question元数据不能借前台session递归ask。目录超过32明确拒绝，tool timeout7205只容纳领域7200上限且仍受原任务deadline。
- 4实际默认unbound双端私聊/Gateway/TxA/link/队列对话测试及61组合通过，五独立CF RED；只读管理UI已随后接齐，但不代表真实账号验收。target自己的ask回答/拒绝候选和Source的ask提问由不同宿主上下文限定，不互借能力。

## 目标侧显式公开通知（待实现，不是现有能力）

权威F8例子“帮我去群里说一声晚上聚餐改到7点”不能由当前只读QuestionModel完成；只完成私有总结并不等于整个F8已验收。目标普通文本/私有ask回答绝不能自动变成QQ消息。

- 拟只为当前首代question worker开放绑定qid的目标本物理群 `say(text)`，无account/endpoint/actor/owner/program/回复窗口参数，不开放递归提问、VM、browser、ops、file、管理或模型配置。问题仍是taint，目标可拒绝；只有目标明确调用say才产生独立公开效果，私有问题/答案本身仍不进公共聊天。
- 使用认证READY/self的真实Bot身份，冻结identity revision、同完整account/目标endpoint/route/capability/stop/budget、原qid/job首代lease/原期限及双边许可。每qid最多一次2000字纯文本、单条、排队最多30秒且不超过原问答截止；稳定承诺不能通过新任务、重启或更换正文重新发送。
- 群必须已确认proactive_state=allowed；unknown/denied明确拒绝，不探测POST、不借A/B任何真人被动消息或后来窗口。预算通知是固定预算专用许可，不能借用；program身份也不可借。需独立新purpose/source及存储约束，旧schema的普通/控制/预算发送保持原路径，不无条件查询新表或捕获缺表。
- enqueue、claim与HTTP前复查同一原runtime和问答/双端current；已接收的QQ效果不可撤回。401/429/格式/主动拒绝、超时/取消及底层retry/plain均不得重放；RAM正文丢失/重启关闭原意图。返回成功只认匹配真实send_ledger applied回执，不把fake native-ID、排队或模型承诺当成功。
- 私有问答结果、公开通知授权终态、迟到真实accepted事实须分开，取消/停止/预算变化后仍保存实际回执但不恢复旧许可/私有答案。启动先关闭旧授权再sender恢复事实；退出排空已accepted事实。管理审计分别展示公开效果与私有回答/原领取，平台接受不等于群友已读。
- 后续分小PR完成schema、纯源/一次计划、Sender接线、目标工具/runtime与实际默认入口测试；独立负对照需覆盖Bot身份、group-only/allowed、首代job/双端current、窗口/重试、一次承诺、取消/迟到回执/恢复。真实QQ主动权限仍由owner测试群验收，不借F7或maxx能力推断。

## 小步实施及验收

1. 连接/独立挑战 schema 与真实PG约束回归；再接认证短码与一次性投递及原命令。
2. 原等待任务与目标 tainted 回合、同工具回答/拒绝、费用及停止/预算/取消边界。
3. 管理台链接和问答视图、原生状态、技能/提示词与固定快照全套。

每步保持小型 Draft PR。优先做跨账号、同端、逆向、伪造角色、旧路由/旧租约、失去权限、迟到/未知投递、重复兑换、重启不重放及任务取消的确定性负例。单元测试使用隔离PG/FakePlatform；测试群双边连接、换说法问答、无链接拒绝及目标 `/stop` 的真实验收仍由 owner 提供范围，不能用替身回执代替。
