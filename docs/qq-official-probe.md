# QQ 官方机器人受控探测（M0）

这个独立命令验证当前账号、环境和指定测试会话的协议行为。它不启动 Gaoji 业务、不调用模型、不迁移历史，也不修改开放平台配置。开发/CI 使用本机假平台；此外，已在用户明确授权的唯一测试群完成下面的真实 API 与产品链路验收。两类证据分别记录，不能把假平台结果当成真实账号能力。

接口结构参考腾讯 SDK `tencent-connect/qqbot-nodejs` 的固定提交 [`429a0f1b798f3b0fcef3679e207dbc0469a1df2d`](https://github.com/tencent-connect/qqbot-nodejs/tree/429a0f1b798f3b0fcef3679e207dbc0469a1df2d)。M0 阶段重新访问 QQ 官方 WebSocket 网页未成功；这个固定 SDK 和既有方案记录不能替代当前账号实测。

## 已有真实证据与工具范围

截至本次 M9 文档整理，已授权群的真实记录包括：

- 文字经持久 M3 发送并取得实际回执；真实 `/help` 的 Markdown 呈现已由用户确认。这不等于表格、代码块和公式各自都已完成呈现验收。
- 一份 **627 字节 PDF** 和一份 **180 字节 ZIP**，分别完成 `srv_send_msg=false` 上传、Type 7 消息发送和 M3 `applied` 回执。用户确认两个文件均已下载并能打开；原始文件哈希已记录，**下载副本的哈希尚未核对**。

这些结果只证明该账号、当时授权目标和上述样本；不能外推到其他群、私聊、尺寸或分片。真实大文件、独立下载反代部署、历史迁移和生产切换没有因此获得通过记录。原始身份、凭据和回执映射保留在受保护记录中，本页不复制。

本 CLI 提供下文列出的文字、观察检查和三个固定小文件检查（`image_upload`、`pdf_upload`、`zip_upload`）。上述已有文件验收使用当时产品的原生上传、持久 M3 和回执链路，**不是后来增加的 CLI 文件检查的实测结果**。CLI 分片检查仍未提供；实际能力和剩余验收见 [运维说明](qq-official.md) 与 [方案第 11 节](qq-official-plan.md#11-用户真机验收清单执行者整理好交给用户)。已有成功记录直接保留，本次文档整理不要求再次发送相同消息或文件。

## 准备独立测试会话

使用新的测试群或自己的测试私聊。先在腾讯后台检查认证、服务范围、接入方式及必要的群主授权。体验名单、IP 白名单是否必需取决于账号实际配置与接口错误，不默认开放公开服务。不要把已有生产实例与本探测工具同时连接到同一个 AppID；先明确停旧接新，或使用独立测试 AppID。

将 AppID、AppSecret 放在受保护的本机文件中，通常使用当前用户所有的 `0600` 文件，并通过实际 ACL 检查；受限的 root/专用服务组读取仅在凭据读取器校验通过时接受。命令不接受直接传入 AppSecret，报告目录也应仅本人可访问。

**此 CLI 的受保护报告写入仍仅支持 Unix。** 当前 `ProtectedReport` 在 `os.name == "nt"` 时直接拒绝，且在发起探测网络操作前创建报告。M8 已为凭据读取增加 Windows 原生身份/句柄/DACL 检查，但没有据此接通探测报告写入；不能将 Windows 凭据读取支持宣传为该 CLI 可在 Windows 完整运行，也不能用 Unix mode 绕过它。下列命令为 Unix 示例。

```bash
umask 077
mkdir -p "$HOME/.local/state/gaoji-qq-probe"
chmod 700 "$HOME/.local/state/gaoji-qq-probe"
# 使用自己的安全编辑器创建 app-id 与 app-secret 文件，然后：
chmod 600 "$HOME/.local/state/gaoji-qq-probe/app-id" \
  "$HOME/.local/state/gaoji-qq-probe/app-secret"
```

不要把真实身份、凭证或原始报告放进源码、issue、PR、聊天或公开日志。无需打开 `AI_QQ_OFFICIAL_ENABLED`；探测是单独的显式命令，应用默认启动不会运行它。

## 先只监听

```bash
python -m src.qq_official.probe \
  --app-id-file "$HOME/.local/state/gaoji-qq-probe/app-id" \
  --app-secret-file "$HOME/.local/state/gaoji-qq-probe/app-secret" \
  --duration 120 \
  --report "$HOME/.local/state/gaoji-qq-probe/listen.json" \
  --raw "$HOME/.local/state/gaoji-qq-probe/identity-local-only.json"
```

默认只连接和监听，**没有 `--checks` 就不发送消息**。在客户端发一条 @ 消息、一条不 @ 的普通消息、一个引用和一个语音消息，检查报告中的事件类型、引用字段与附件字段。收到 `GROUP_MESSAGE_CREATE` 可以证明当时该端点确实推送了该事件，不能单凭它推断其他群也有全量权限。没有收到某种事件也不证明永久不支持。

`--raw` 是可选的本机身份映射文件，只保存假名对应的原始 AppID/openid/消息或引用 ID，便于可信控制台首次指定 owner。它不会保存消息正文、ASR、签名 URL、token 或密钥，不打印到控制台，也不能授予 owner 权限。没有这个参数时，不写原始身份映射。

报告和身份映射都是新建的 `0600` 文件，拒绝覆盖现有文件或跟随符号链接。写入前按已打开的文件描述符检查实际 ACL，包括每次原子替换用的新临时文件；继承的扩展 ACL 或无法核实的权限模型会被拒绝，避免 mode 为 `0600` 却仍可被其他用户读取。每次运行使用新文件名。Ctrl+C 会关闭测试连接，并保存已经观察到的脱敏结果。

## 显式选择发送项目

下面 `GROUP_OPENID_TEST_1` 是明显的假值；使用前从受保护的本机身份文件确定自己的测试目标，不能凭群名猜测。私聊用 `--user`；可同时列出一个群和一个私聊，但它们的结果分别记录。

```bash
python -m src.qq_official.probe \
  --app-id-file "$HOME/.local/state/gaoji-qq-probe/app-id" \
  --app-secret-file "$HOME/.local/state/gaoji-qq-probe/app-secret" \
  --group GROUP_OPENID_TEST_1 \
  --checks reply,non_at,proactive,markdown_table,markdown_code,markdown_formula \
  --duration 300 \
  --report "$HOME/.local/state/gaoji-qq-probe/text.json"
```

工具会先显示目标假名、探测项目和本批最多发送请求数，输入一次 `YES` 后执行。变更目标或增加项目须重新运行并确认。受控非交互运行可以显式传 `--confirm-batch`，其授权仅限本次命令里列明的目标与项目。

| 项目 | 影响 | 需要的测试输入 |
| --- | --- | --- |
| `image_upload` | 最多 1 次 PNG 上传 + 1 条 Type 7 消息 | 一个新的 @/私聊消息；上传不自动发送 |
| `pdf_upload` | 最多 1 次 PDF 上传 + 1 条 Type 7 消息 | 另一个新的 @/私聊消息；随后人工下载核对 |
| `zip_upload` | 最多 1 次 ZIP 上传 + 1 条 Type 7 消息 | 再一个新的 @/私聊消息；随后人工下载核对 |
| `reply` | 最多 1 条被动文字回复 | 群发新的 @ 消息；私聊发新消息 |
| `non_at` | 最多 1 条被动文字回复 | 群发新的不 @ 消息；私聊跳过 |
| `proactive` | 最多 1 条主动消息 | 不使用消息窗口，不修改主动发言开关 |
| `markdown_table` | 最多 1 条 Markdown 表格 | 新的 @/私聊消息；随后人工检查表格 |
| `markdown_code` | 最多 1 条 Markdown 代码块 | 新的 @/私聊消息；随后人工检查代码块 |
| `markdown_formula` | 最多 1 条 Markdown 公式 | 新的 @/私聊消息；随后人工检查公式 |
| `dedup` | 同一 payload、msg_id、msg_seq 请求两次 | 单独选择；故意触发重复检测 |
| `length` | 最多发送一条 2001 字测试文字 | 单独选择；可能被长度限制拒绝 |
| `window_limit` | 群最多 6 次、私聊最多 5 次回复请求 | 单独选择；故意触发窗口次数限制 |

文件检查只使用工具内固定、无害且各不超过 1 KiB 的 PNG/PDF/ZIP；不接受本机路径、URL、用户文件或自定义附件正文。图片为一个像素，PDF 只有测试文字，ZIP 只有一份 `README.txt`。每项使用一次 small-upload，明确 `srv_send_msg=false`，确认上传回执后才用同一原消息窗口发送一条 Type 7；上传成功不会被记成消息已发送或已下载。两个目标分别计数，三个文件对一个目标最多 **3 次上传 + 3 次消息请求**，鉴权和网关请求不属于这六次业务请求。

```bash
python -m src.qq_official.probe \
  --app-id-file "$HOME/.local/state/gaoji-qq-probe/app-id" \
  --app-secret-file "$HOME/.local/state/gaoji-qq-probe/app-secret" \
  --group GROUP_OPENID_TEST_1 \
  --checks image_upload,pdf_upload,zip_upload \
  --duration 300 \
  --report "$HOME/.local/state/gaoji-qq-probe/files.json"
```

按提示发送三个不同 ID 的新 @ 消息。私聊使用 `--user`，同样逐项用新窗口。报告分别记录 `stage=upload` 与 `stage=send`、固定样本的 SHA-256/字节数/MIME、脱敏结果及错误；原生 `file_info`、`file_uuid` 和 `raw_url` 不写报告或原始身份映射。下载后可把副本 SHA-256 与 `sample_sha256` 比较，未下载或未核对只保留“平台接受、人工核验待完成”。

上传结束后、token 准备以及实际发送边界再次检查**原**窗口、上传凭据和整批截止；耗时不会换来新期限。上传不完整/结果不明/隐含已发送证据均停止该目标剩余项目，不上传第二次、不换序号、不切链接。明确拒绝也不自动重试该项。若窗口在上传后过期，保留已上传记录并标消息未执行；不能重新取一个新窗口补发这次探测。

不同被动探测项必须取得**不同消息 ID 的新窗口**；@、非 @ 和私聊分别检查。没有有效时间戳、已过期、时间明显超前、机器人回声或重复事件不能提供新的探测窗口。每次 HTTP 请求前再次检查窗口期限，包含重复/超限探测里的后续请求，避免把网络等待导致的过期误判成次数限制。探测只发送固定测试文案，不把真实入站正文回传给接口。

不默认选择重复、超限或长度探测。工具不会在错误后更换序号补发、切格式绕过拒绝，或切账号/渠道。若一次发送结果不明，当前端点后续项目标为 `not_run / previous_send_ambiguous`，先核对客户端与报告。

`--sandbox` 显式选择独立沙箱环境，候选地址为 `https://sandbox.api.sgroup.qq.com`。当前账号的沙箱地址/权限契约仍需核实，报告会标注 `sandbox_address_requires_account_verification`；不会静默从生产切到沙箱。不要将生产凭证发送到自定义主机。供自动测试使用的隐藏 loopback 参数只允许字面本机地址与 `TEST_` 假凭证，不能携带真实凭证。

## 读报告与反馈

每份报告包含账号/环境、目标类别、观察时间、事件类型、各次发送阶段、HTTP 状态/腾讯错误码、脱敏回执、网关关闭码和恢复缺口。身份假名在同一报告里稳定，重新运行会换一个私有随机盐，避免公开报告之间自动关联。

- `accepted`：获得实际消息 ID 回执。Markdown 另标 `manual_verification_required`，需要在客户端检查呈现；不能把 HTTP 成功当格式验收通过。
- `rejected`：有明确未接受证据，保留错误码。主动权限拒绝只证明该端点当时的状态。
- `ambiguous`：请求可能已经接受，或者未知错误无法确定。没有成功回执，不把重复错误当原消息成功凭据。
- `not_run`：缺少新的合法窗口、前一次结果不明等；属于待验收。
- `connection_failed`：网关鉴权、配置或重连预算失败。查看 `gateway` 内原始关闭码与脱敏原因；程序停止前的诊断会保留。
- `self_echo=observed_matching_receipt`：在同端点看到与出站回执匹配的消息。`not_observed` 只表示本次未观察到，不能断言平台不推回消息。

报告不包含聊天原文、ASR 文本、密钥、绑定码或下载签名。`samples` 最多保存 20 种消息结构的脱敏 Dispatch，保留递归引用关系、字段结构和身份假名，正文/文件名/URL 均替换成固定测试占位；未知字段丢弃。这些样本可作为 `tests/fixtures/qq_official/` 的新增夹具，但提交前仍应人工审查报告。

本 CLI 已提供固定小图片/PDF/ZIP 检查；分片上传、撤回、语音发送仍在 `pending` 中标为 `not_implemented`。产品分片已有离线实现，但真实目标证明仍须单独取得；不由小文件成功推导。真实禁言、移除成员、生产重启不属于默认探测。

将脱敏报告与**版本、账号环境、目标范围、测试时间及人工呈现检查结论**一起保留。只对实测成功的能力与端点记录证据，未测项保持未知；文字探测通过不等于可以切生产。

## F7 群管探测准备（固定协议，尚未实测）

`src/qq_official/group_moderation.py` 仅提供受限的宿主协议底座，尚不是群管探测 CLI，也未注册成 `moderate` 或 VM 工具。固定路径与候选契约来自 maxx 设计记录，不代表当前 Gaoji 账号拥有这些能力。下一步独立探测须由 owner 明确选择测试账号、唯一测试群、同意测试的普通成员及新测试消息；不连接或复用线上实例。

- 读取仅 `GET /v2/groups/{group}/bot_state` 与 `GET .../restrict_chat_setting`；写入仅后一路径的 `POST members=[{op,member_openid,mute_expire_at}]`，一次一个成员，`op=add/update/del`，不修改全群禁言、移除成员或黑名单。
- 解除使用 `del` 与空 expiry；正时长为整数 1–2592000 秒，RFC3339 `+08:00`。这只是平台候选上限：产品仍须核实原认证请求人角色并限制管理员 1 天、群主 30 天；不能由该协议函数自行授予权限。
- 撤回只用 `DELETE /v2/groups/{group}/messages/{event_id}`。候选成功契约允许 HTTP 200 空正文，仅此固定操作例外；其他读取/禁言/发消息仍拒绝空或坏 JSON，未声明的 204 等状态保持未知。HTTP 200 的非零错误码仍是拒绝。
- 不接受自定义地址、批量对象或不规范路径，不在认证失败、限流或不明响应后重试。每次实际 HTTP 保留现有源/期限 guard，不能用 token 等待换新期限。原始响应只留宿主内，不自动写日志或公开报告。

71 项协议、既有 HTTP 与原探测/文件探测组合回归通过（含此前 38 项）；恢复旧 JSON-only 撤回解析的负对照稳定失败。它们仅为本机替身结果。真实账号权限、短暂禁言及恢复、普通成员消息撤回、无管理员权限拒绝均仍待探测，不把 maxx 的成功记录或 HTTP 替身当作 Gaoji 实测。

### F7 单成员探测引擎（CLI 尚未接通）

`probe_moderation.py` 在确认前不连接，固定账号/群/普通参与者；只接受该成员的新鲜原始纯文本 @ 消息和当次随机测试文案。不同步骤使用不同消息 ID，转发、引用、其他人/群、管理员/群主/机器人或旧消息不能作测试来源。批次最多五分钟、原消息最多两分钟，每个实际请求（含 token/TLS 等待后）复查，不续期。

先查询 bot 当前管理员角色与身份。禁言实验只做 60 秒，拒绝修改已有规则；检查实际规则期限后才尝试解除，再 GET 核实，撤回另需一条新测试消息。清理前规则改变则停止，不删除新规则。QQ 没有 CAS，**测试窗口内不得并行进行人工群管**；检查不能保证外部管理员与最后 DELETE/POST 间的原子隔离。响应不明或中断后不重试、不自动补解除，临时禁言等待原期限自行结束。

报告区分请求接受、GET 核实和未执行/未知，使用假名，不保存消息原文、openid、响应体或凭据；客户端观察仍须人工确认。引擎与协议/旧探测组合 81 项本机回归通过，含此前 71 项；尚无 CLI 和当前账号实测结果，不能以本机结果开放产品 `moderate`。

### F7 显式 Unix 命令入口

入口为 `python -m src.qq_official.probe_moderation_cli`，不是 Gaoji 启动选项或模型/VM 工具。默认 `--checks read` 只查 bot 状态与禁言规则；`mute,recall` 必须明确选择。凭据、scope 必须是当前操作者所有、无 group/other 权限的普通文件，拒绝符号链接/超长输入；新报告放在操作者私有目录。Darwin 按已打开 FD 调用固定系统 `ls -e` 检查扩展 ACL，发现或无法核实则拒绝；Linux 依赖私有 mode 的 ACL mask。Windows/未知 ACL 平台不开放此入口，不改写既有凭据读取策略或 Windows 待修问题。

先在**独立测试账号**对应的受保护 `scope.json` 写入以下结构（全部 `TEST_…` 为假值，真实值只留私有文件）：

```json
{"app_id":"TEST_APP_ID","group":"TEST_GROUP","member":"TEST_MEMBER","independent_test_account":true,"exclusive_moderation":true}
```

`app_id` 必须匹配独立凭据文件；两个布尔值是操作者的明确范围声明，不是平台能力证明。不能复用正在运行的线上 AppID，不能选择未同意测试的成员。对目标的确认依赖这份私有映射；终端只显示稳定假名、步骤及最多业务请求数。测试窗口内不进行并行人工群管。私有目录/文件示例权限为 `0700`/`0600`，不在命令行、PR 或聊天粘贴真实身份或密钥。

```bash
python -m src.qq_official.probe_moderation_cli \
  --app-id-file "$HOME/.local/state/gaoji-f7-test/app-id" \
  --app-secret-file "$HOME/.local/state/gaoji-f7-test/app-secret" \
  --scope-file "$HOME/.local/state/gaoji-f7-test/scope.json" \
  --report "$HOME/.local/state/gaoji-f7-test/new-report.json" \
  --checks mute,recall --duration 300
```

输入一次 `YES` 后，再让指定普通成员按提示发当次文案；两个动作需要不同的新消息 ID。最大全流程为 10 次业务请求（不含鉴权/网关）；默认只读 2 次，另加只读检查最多 12 次。`--confirm-moderation-batch` 仅替代本次交互确认；不续期、不签发能力证明。`--sandbox` 只选择固定沙箱地址，失败不切到另一环境。

CLI/引擎/既有 HTTP、消息与文件探测组合 **86 项通过**（含此前 81 项），Ruff/导入和实际 `--help` 通过。尚未调用真实账号，未完成 F7 平台探测或产品 `moderate`；新工具尚无最新完整快照验证，不能借用 F9 的 2,028 项全套。

## 剩余受控验收与记录

按 [方案第 11 节](qq-official-plan.md#11-用户真机验收清单执行者整理好交给用户) 使用既有受保护控制面、产品入口和人工检查继续验收，不另建验收会话系统或自动签发证明。以下步骤不会由阅读本文自动执行；仍受原唯一群范围及具体操作授权约束，增加私聊、普通身份或其他目标时须先明确允许的范围。用户未参与的人工呈现、下载或身份步骤保持待验收，不推断为成功。

| 对应步骤 | 尚需保留的真实结果 |
|---|---|
| 1：协议报告 | 保留已有文字报告；仅补确实未测且获准的项目。每条被动检查使用新的合法窗口，结果不明时先核对，不重放。 |
| 2：owner 与新群绑定 | 用可信控制台核对 C2C owner、双向群绑定和普通身份无法抢占 owner；正常聊天不能重复建立绑定。需额外身份参与的部分先确认测试范围。 |
| 3、13：迁移、回退与启动模式 | 先用脱敏旧记录或获准测试来源，核对预览、映射和 grant；验证官方单平台、双平台、未迁移 OneBot 不受影响、已迁移入口停止及显式回退。未授权的生产迁移仍不执行。 |
| 4–7：上下文与媒体呈现 | 非 @ 连续聊天后 @ 提问、引用机器人回复继续追问；分别检查图片/语音、表格/代码块/公式。真实 `/help` 已确认不能替代这些逐项检查。 |
| 8：长任务 | 使用真实 Sub-Agent 运行超过 5 分钟；分别记录主动出口允许与拒绝后的结果状态，拒绝时只在后来取得合法窗口后交付。非 @ 是否提供窗口须实际观察。 |
| 9：文件、期限与清理 | 保留已完成的小 PDF/ZIP 上传、M3 回执和人工打开结论；尚缺下载副本哈希比对。若原副本仍在，可直接核对，无需重新发送。另验收新获准尺寸/分片、下载链接期限和对应副本清理、重启不重复发送；自建下载反代尚未真实部署。 |
| 10：后台时效 | 观察提醒、例行的发送/等待状态；过时插话不补发，原截止到期有明确原因。 |
| 11、14：授权边界 | 仅用无害操作核对任务范围内继续、普通身份拒绝、撤权/超范围拒绝、项目任务恢复和文件发送前撤权；近期发言不能当完整成员资格。 |
| 12：连接恢复 | 仅中断获准测试进程的连接或重启测试实例，核对事件去重、缺口显示、`unknown` 保留且不自动补发；不修改主机网络。 |

每项结果记录精确代码版本/测试模式、账号环境与受保护目标引用、操作范围、观察时间、原窗口和截止、实际请求及适用的计划/回执关联与结论。文件另记 MIME、字节数、原始哈希、下载副本哈希（未核对则明确标记）、人工打开结论、到期与实际清理结果。公开摘要只用脱敏引用，不包含 openid、正文、凭据、原生文件句柄或签名 URL。

区分“配置解析通过”“本机假服务通过”“平台接受”“客户端呈现/下载确认”。缺证据用待验收或未知，`unknown` 不能标成失败后自动重试；人工确认也不能替代未取得的哈希或回执。已有真实成功记录继续沿用原版本、目标和时间，不通过更新文档刷新其权限或期限。
