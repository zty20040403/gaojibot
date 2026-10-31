# gaoji

QQ 官方机器人，使用 PostgreSQL 保存聊天历史、记忆、任务和发送回执。支持多模型、分段回复、文件交付和每个会话独立的持久 Linux VM。

## 当前功能

- 群聊与私聊，管理员绑定、群绑定及模型切换。
- 按自然段发送回复；`say` 报告真实进度；`[silence]` 保持安静。
- 稳定的聊天窗口、`history` 查询原文、会话记忆和每天的记忆整理。
- 后台 `agent` 由本群 VM 里的 Codex 执行研究、代码、文件和视频任务，同时最多三个，每个最长两小时；支持查询、插话、反问和取消。完成后由前台交付结果。后台模型和推理强度在控制台「任务与投递」页修改，默认 `gpt-6.1-sol`。
- 一次性提醒和周期任务，时间按 Asia/Shanghai；周期最短一小时。
- 工作区内 Chromium、Playwright、网页和 PDF 正文读取、截图理解、ffmpeg 和 Whisper。
- 表情包、图片理解、附件导入、文件上传、中文语音回复。
- 管理员私聊发起的后台任务可通过宿主 `ops_read` 只读查看服务器。
- 管理台可查看聊天、用量、任务及最近模型请求。

GitHub 机器人账号和自动提 PR 功能暂缓。旧 OneBot/NapCat、项目工作区、群百科、固定消息、MCP、桥接和多角色 Sub-Agent 已移除。旧实现保存在 `pre-rework-20261031` 标签。

## 运行

需要 Python 3.12+、PostgreSQL 17（启用 pgvector）、官方 QQ 机器人凭据和模型 API。文件制作推荐在 Linux KVM VM 后端运行。

```sh
uv sync
cp .env.example .env
# 配置数据库、模型，以及受保护的 QQ 凭据文件路径。
uv run python -m src.bot_storage.cli upgrade
uv run python bot.py
```

密钥放在宿主私有文件或环境中，不提交仓库，不传入 VM。官方 QQ 的 AppID、AppSecret 和绑定 pepper 通过文件读取；文件权限应为 `0600`。管理员身份来自已核对的官方私聊事件，普通 QQ 号不是 openid。

配置入口：

- [.env.example](.env.example)：常用运行参数。
- [官方 QQ 接入](docs/qq-official.md)：绑定和发送行为。
- [宿主配置](docs/qq-official-host-config.md)：文件、媒体、主动接话。
- [上线步骤](docs/deploy-rework.md)：统一部署和历史数据调整。
- [交接记录](docs/HANDOFF.md)：W1–W10 的范围与完成记录。

## NixOS

Flake 提供 `packages.<system>.default`、`sandbox-image` 和 `nixosModules.default`，以及独立的 cluster-control/worker 模块。

```nix
services.gaoji = {
  enable = true;
  environmentFile = "/run/secrets/gaoji.env";
  qqOfficial = {
    enable = true;
    appIdFile = "/run/secrets/qq-app-id";
    appSecretFile = "/run/secrets/qq-app-secret";
    bindPepperFile = "/run/secrets/qq-bind-pepper";
    hostConfigFile = "/run/secrets/qq-host.json";
  };
};
```

持久工作区设置 `sandbox.enable = true`、`sandbox.backend = "vm"`，提供固定版本的 Debian cloud image 和 `sandbox.vmRoot`。新 VM 预装文件、浏览器和影音工具；已有 VM 在部署时显式升级，文件继续保留。`opsRead.baseUrl/tokenFile` 仅供宿主读取；`voiceAsr.enable` 可开启短语音的本地转写回退。

## 开发检查

```sh
source tools/dev/testenv.sh gaoji_test
python -m unittest discover -s tests -p 'test_*.py'
ruff check bot.py src tests tools migrations nix
python tools/dev/check_unresolved_imports.py src
cd admin-ui && npm ci && npm run build
```

测试使用独立的本地 PostgreSQL 库，不使用正式 QQ 凭据。修改时只增加能验证实际行为的检查，删除功能时同步删除其测试。真实平台能力另行核验，不能把模拟回执当作真实送达。
