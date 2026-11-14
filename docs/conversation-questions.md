# F8 双边连接与跨会话问答

状态（2026-11-14）：连接存储、可撤销权限证据、独立敏感输入隔离、纯双边reducer、原入站/单次发行计划schema、可选原认证TxA及一次ephemeral发送/实际账本取证已建立；定向管理、原回复证据、一次控制反馈及真实账号factory/dispatch/monitor/recover/close已接，`/link` 控制本地可用；`ask`、目标模型与管理页尚待实现，未真实QQ验收或合并/部署。权威功能要求见 `next-features-plan.md` F8，参考 maxx 固定设计的 `Asking another conversation`。

## 连接不是身份映射

- 单向 A→B 只允许 A 的 bot 向 B 的 bot 提问；没有反向权利。连接面向实际物理 endpoint，不是 canonical 历史、姓名、人员编号或跨群 openid 的推测。
- 仅同一官方账号（完整 AccountKey，包括 scope）的已准入会话；不提供跨宿主账号访问。源/目标均需当前路由与成员状态有效。历史别名不是额外授权，也不把路由改为对端历史。
- `/link` 在 A 由当前宿主 owner 或原认证群主/群管申请；私聊不把普通 peer 当作全局 owner。B 的 owner/原认证群管在 B 输入 `/link <code>` 才确认 A→B。相同 endpoint 不能兑换；重复确认不重复建立，反向要另做一次。
- 普通成员不能因知道验证码就建立连接；引用、转发、模型文本、网页、文件、VM 请求与裸 role 字段都不是认证命令。禁止自动按成员关系建立边。
- LinkManagement的list/status/cancel/revoke已接真实账号原认证TxA，仅原认证owner/群管读写自己物理端点涉及的同账号记录，查找后复查原600秒期限。list在LIMIT前限定范围，最多32条及more；只返回散列会话标签、方向/状态/版本，不泄露其他账号/会话的openid。控制反馈与真实账号工厂已接，本地可验而非QQ实测。
- 默认owner私聊不依赖/bind历史绑定：新private/unbound/present且canonical scope为原物理私聊时可核验owner grant/准入；不放开unbound群、普通public peer、revoked或unbound外部历史。一次link控制在主动状态尚未明确allowed时只选择原认证msg_id窗口，不先探测失败再重试；已有明确allowed仍遵守一次尝试。默认store入口及真实账号factory已用loopback验，本地结果不代表QQ权限。
- `/unlink` 和管理台撤销提供明确方向；任一端可撤销涉及自己的授权，不能删除无关边。现有链接保留独立版本，重建不能复活旧问答。

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

- `ask` 提交有限长度的问题，立即返回独立等待任务编号；截止不超过两小时，使用原 source current。无连接和不存在的目标统一拒绝，不枚举他人会话。
- B 只收到问题及安全的来源会话标签，不收到 A 的历史、记忆、文件、原 actor 的 owner/群管授权或被动回复窗口。B 的模型/历史/账单归 B 的物理会话；跨会话来源须独立 taint，不能冒充真人或常驻程序。
- 目标按自身规则回答或拒绝；来源及问题作为 untrusted 资料进入易变上下文。不得挂载需要原 owner 真人私聊的 ops、浏览器个人授权、文件/VM 权限等工具。连接不放开 VM 的跨会话接口，也不复制另一台 `/work`。
- 问答及模型内部通知只在等待任务/回合审计中保存，不是公共聊天消息，不进入 `history` / VM events。目标普通发言仍须显式工具动作与自身主动消息权限/额度/停止门槛；不能把问题、回答或“已收到”自动作为 QQ 通知。
- 原等待任务、链接版本、双端物理路由、双端停止代数、目标自动动作预算代数及账号 epoch 在排队、模型等待、返回和通知边界复查。`/stop`、取消、撤链接、过期或重启关闭旧问答；恢复仅接受新请求，不重放已读结果/未知效果。
- 管理台只向登录管理员按宿主账号列出链接/方向/当前状态和问答记录，写入使用 CSRF、独立资源版本与事务审计，不提供代替 QQ 双边确认的通道。

## 小步实施及验收

1. 连接/独立挑战 schema 与真实PG约束回归；再接认证短码与一次性投递及原命令。
2. 原等待任务与目标 tainted 回合、同工具回答/拒绝、费用及停止/预算/取消边界。
3. 管理台链接和问答视图、原生状态、技能/提示词与固定快照全套。

每步保持小型 Draft PR。优先做跨账号、同端、逆向、伪造角色、旧路由/旧租约、失去权限、迟到/未知投递、重复兑换、重启不重放及任务取消的确定性负例。单元测试使用隔离PG/FakePlatform；测试群双边连接、换说法问答、无链接拒绝及目标 `/stop` 的真实验收仍由 owner 提供范围，不能用替身回执代替。
