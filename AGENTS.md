# gaojibot 开发须知

官方 QQ 群聊机器人。架构决策见 `docs/adr/`，沙盒结构见 `docs/container-sandbox.md`。

## 红线

- 这是公开仓库。不要提交真实的 QQ 号、群号、openid、AppID/AppSecret、token、主机名或内网地址；测试一律用 `TEST_…` 这样的假值。
- 不部署、不连生产服务器、不改生产库。这些都由 owner 来做。
- 任何密钥、token、ssh key 都不能放进沙盒容器：群友能让 AI 在容器里执行任意命令。需要密钥的操作由宿主上的工具代为调用。
- 不要恢复已经删除的功能（项目工作区、群百科、固定消息、MCP、桥接、桌面 App、OneBot）。旧代码在 tag `pre-rework-20261031`，只作参考。

## 做法

- 先管模型看到什么（提示词、上下文、记忆、工具说明），再考虑加代码。
- 删代码优先，不写兼容层。多步操作、跑命令、跑很久的事交给后台 agent，不要做成前台专用工具。
- 维护者一件事一个 commit，手写改动约 400 行以内；本地跑过相关测试和 ruff 后直接推 main，不开 PR。CI 不是测试机，main 变红先修再推新的。每项都要在测试群用真实对话验收。
- 过程记录、交接和报告不提交进仓库。
- 出错要报出来，不要静默降级。

## 命令

- 测试环境：`source tools/dev/testenv.sh <库名_test>`；全套测试：`python -m unittest discover -s tests -p 'test_*.py'`（约 10 分钟）。
- 代码检查：`ruff check bot.py src tests tools migrations nix`。
- 删代码后跑 `tools/dev/` 里的导入检查，并 grep 按文件路径读取的 `.py` 文件（例如 `nix/guest/tools.nix` 会打包 `sandbox_pdf.py`）。
