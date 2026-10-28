# QQ 官方机器人受控探测（M0）

这个独立命令验证当前账号、环境和指定测试会话的协议行为。它不启动 Gaoji 业务、不调用模型、不迁移历史，也不修改开放平台配置。当前开发测试只使用本机假平台，**尚无本账号的真实 API 验收结果**。

接口结构参考腾讯 SDK `tencent-connect/qqbot-nodejs` 的固定提交 [`429a0f1b798f3b0fcef3679e207dbc0469a1df2d`](https://github.com/tencent-connect/qqbot-nodejs/tree/429a0f1b798f3b0fcef3679e207dbc0469a1df2d)。本次重新访问 QQ 官方 WebSocket 网页失败；SDK 和既有方案记录不能替代当前账号实测。

## 准备独立测试会话

使用新的测试群或自己的测试私聊。先在腾讯后台检查认证、服务范围、接入方式及必要的群主授权。体验名单、IP 白名单是否必需取决于账号实际配置与接口错误，不默认开放公开服务。不要把已有生产实例与本探测工具同时连接到同一个 AppID；先明确停旧接新，或使用独立测试 AppID。

将 AppID、AppSecret 放在受保护的本机文件中，文件必须由当前用户拥有、权限为 `0600`。命令不接受直接传入 AppSecret。报告目录也应仅本人可访问。当前探测报告写入仅支持 Unix；Windows 在 ACL 支持接通前会拒绝执行，不能把 Unix mode 当 Windows 保护。

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
| `reply` | 最多 1 条被动文字回复 | 群发新的 @ 消息；私聊发新消息 |
| `non_at` | 最多 1 条被动文字回复 | 群发新的不 @ 消息；私聊跳过 |
| `proactive` | 最多 1 条主动消息 | 不使用消息窗口，不修改主动发言开关 |
| `markdown_table` | 最多 1 条 Markdown 表格 | 新的 @/私聊消息；随后人工检查表格 |
| `markdown_code` | 最多 1 条 Markdown 代码块 | 新的 @/私聊消息；随后人工检查代码块 |
| `markdown_formula` | 最多 1 条 Markdown 公式 | 新的 @/私聊消息；随后人工检查公式 |
| `dedup` | 同一 payload、msg_id、msg_seq 请求两次 | 单独选择；故意触发重复检测 |
| `length` | 最多发送一条 2001 字测试文字 | 单独选择；可能被长度限制拒绝 |
| `window_limit` | 群最多 6 次、私聊最多 5 次回复请求 | 单独选择；故意触发窗口次数限制 |

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

M0 提供文字和观察性检查。图片、PDF、zip、分片上传、撤回、语音发送在报告的 `pending` 中明确标为 `not_implemented`，分别由 M6/M7 扩展。真实禁言、移除成员、生产重启不属于默认探测。

将脱敏报告与**版本、账号环境、目标范围、测试时间及人工呈现检查结论**交回。只对实测成功的能力与端点记录证据，未测项保持未知。之后再用新测试会话完成身份绑定、文件实际下载/哈希、恢复和迁移/回退验收；文字探测通过不等于可以切生产。
