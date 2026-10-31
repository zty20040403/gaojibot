# gaojibot 开发须知

官方 QQ 群聊机器人。正在进行的改造先读 `docs/HANDOFF.md`，总体方案见 `docs/gaoji-rework-plan.md`。

## 红线

- 这是公开仓库。不要提交真实的 QQ 号、群号、openid、AppID/AppSecret、token 或内网地址；测试一律用 `TEST_…` 这样的假值。
- 不部署、不连生产服务器、不改生产库。这些都由 owner 来做。
- 任何密钥、token、ssh key 都不能放进沙盒 VM：群友能让 AI 在 VM 里执行任意命令。需要密钥的操作由宿主上的工具代为调用。
- 不要恢复已经删除的功能（项目工作区、群百科、固定消息、MCP、桥接、桌面 App、OneBot）。旧代码在 tag `pre-rework-20261031`，只作参考。

## 做法

- 先管模型看到什么（提示词、上下文、记忆、工具说明），再考虑加代码。
- 删代码优先，不写兼容层。多步操作、跑命令、跑很久的事交给后台 agent，不要做成前台专用工具。
- 每个 PR 只做一件事，手写改动约 400 行以内；每项都要在测试群用真实对话验收。
- 出错要报出来，不要静默降级。

## 命令

- 测试环境：`source tools/dev/testenv.sh <库名_test>`；全套测试：`python -m unittest discover -s tests -p 'test_*.py'`（约 10 分钟）。
- 代码检查：`ruff check bot.py src tests tools migrations nix`。
- 删代码后跑 `tools/dev/` 里的导入检查，并 grep 按文件路径读取的 `.py` 文件（例如 `vm_sandbox.py` 会读 `sandbox_pdf.py`）。
