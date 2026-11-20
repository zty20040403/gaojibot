<p align="center">
  <img src="docs/assets/gaoji-banner.svg" alt="gaoji：聊天像朋友，干活像 agent。开源的 QQ 官方机器人多模型 AI 群成员，有记忆、有工具，还有沙箱里的后台 agent。" width="100%" />
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;·&nbsp; <strong>简体中文</strong>
</p>

<p align="center">
  <a href="https://github.com/zty20040403/gaojibot/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/zty20040403/gaojibot/ci.yml?branch=main&amp;style=flat-square&amp;label=CI&amp;labelColor=0A0D12&amp;color=34D399" alt="CI 状态" /></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-38BDF8?style=flat-square&amp;logo=python&amp;logoColor=EEF2F7&amp;labelColor=0A0D12" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/PostgreSQL-17-38BDF8?style=flat-square&amp;logo=postgresql&amp;logoColor=EEF2F7&amp;labelColor=0A0D12" alt="PostgreSQL 17" />
  <img src="https://img.shields.io/badge/Deploy-NixOS-38BDF8?style=flat-square&amp;logo=nixos&amp;logoColor=EEF2F7&amp;labelColor=0A0D12" alt="NixOS 部署" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-38BDF8?style=flat-square&amp;labelColor=0A0D12" alt="MIT 许可" /></a>
  <a href="https://github.com/zty20040403/gaojibot/stargazers"><img src="https://img.shields.io/github/stars/zty20040403/gaojibot?style=flat-square&amp;logo=github&amp;logoColor=EEF2F7&amp;label=Stars&amp;labelColor=0A0D12&amp;color=38BDF8" alt="GitHub stars" /></a>
</p>

<p align="center">
  <b>gaoji</b> 是住在你 QQ 群里的 AI 群成员：聊天像个人，干活像个 agent。<br />
  @ 它一定回，没话说就安静，记得群里的事，<br />
  真要干活时，交给这个会话专属沙箱容器里的编程 agent。
</p>

<p align="center">
  <a href="#highlights">亮点</a> &nbsp;·&nbsp;
  <a href="#how-it-works">工作原理</a> &nbsp;·&nbsp;
  <a href="#quick-start">快速开始</a> &nbsp;·&nbsp;
  <a href="#deploy-with-nixos">NixOS 部署</a> &nbsp;·&nbsp;
  <a href="#security">安全</a> &nbsp;·&nbsp;
  <a href="#documentation">文档</a>
</p>

<br />

<a id="highlights"></a>
<p align="center">
  <img src="docs/assets/gaoji-highlights.svg" alt="亮点：知道什么时候该说话；记得住；在沙箱里真干活；能看、能听、能画；按时执行；安全设计。" width="100%" />
</p>

### 为什么是 gaoji

|  | 常见的聊天机器人 | **gaoji** |
| :--- | :--- | :--- |
| **什么时候说话** | 什么都回，或者只认命令 | @ 它、私聊它一定回；其他时候有用才开口 |
| **记忆** | 只记得最近几条 | 每个会话独立记忆，能翻全部原始记录，给常来的人各记一张印象卡，定期整理 |
| **长任务** | 卡住聊天，或者直接超时 | 持久化后台任务，边聊边查进度、追加要求或取消 |
| **代码在哪跑** | 在机器人宿主上，或者根本不能跑 | 在会话自己的 NixOS 容器里，`/work` 持久保留，没有宿主密钥，进不了内网 |
| **发送** | 发出去就不管了 | 持久化发件箱加平台真实回执，**生成了不算送达** |
| **模型** | 只有一家 | 多家模型，每个会话可以选不同的 |

### 实际用起来是这样

| 你说 | gaoji 做 |
| :--- | :--- |
| *「对比一下这个月的内存价格，给我图表和 CSV。」* | 先说开始了 → 后台任务搜索并处理数据 → 发回带来源的结论和文件。 |
| *「这个视频讲了什么？」* | 导入附件 → 在沙箱里抽帧、转写音频 → 回一段总结。 |
| *「刚才那个再加上另一个模型。」* | 找到正在跑的任务追加要求，不会重复开一个。 |
| *「明天下午三点提醒我开会。」* | 建一个一次性提醒，到点准时发。 |

<sub>以上是流程示例，不是截图。实际效果取决于模型、宿主配置和 QQ 给的权限。</sub>

<a id="how-it-works"></a>
## 工作原理

<p align="center">
  <img src="docs/assets/gaoji-architecture.svg" alt="架构：QQ 官方消息进入前台 agent，前台调用多个模型，通过持久化发件箱回复。长任务变成持久化后台任务，在会话自己的容器里运行 agent。PostgreSQL 保存账本。" width="100%" />
</p>

- **前台**：QQ 官方消息 → 时间线和记忆 → 多模型 agent → 唯一一条持久化发送路径。
- **后台**：持久化任务 → 宿主驱动编程 agent → 会话容器里的 `/work` → 汇报唤醒前台，用同一个人设发出去。
- **状态**：PostgreSQL 保存消息账本、记忆、任务、定时任务和发送回执。

<a id="quick-start"></a>
## 快速开始

**需要：** Python 3.12+、[uv](https://docs.astral.sh/uv/)、带 pgvector 的 PostgreSQL 17、QQ 官方机器人凭据和一个模型 API。要用文件、浏览器和视频功能，需要一台开了 KVM 的 Linux 主机。

```sh
git clone https://github.com/zty20040403/gaojibot.git
cd gaojibot
uv sync --locked
cp .env.example .env          # 数据库、模型、QQ 凭据文件路径
uv run python -m src.bot_storage.cli upgrade
uv run python bot.py
```

| 配置什么 | 看哪里 |
| :--- | :--- |
| 数据库、模型、人设、搜索、沙箱、后台任务 | [`.env.example`](.env.example) |
| 接入和绑定 QQ 官方机器人 | [QQ 官方接入](docs/qq-official.md) |
| 文件、媒体和主动接话 | [宿主配置](docs/qq-official-host-config.md) |

> [!IMPORTANT]
> AppID、AppSecret 和绑定 pepper 从宿主上权限为 `0600` 的文件读取。不要提交密钥、真实 QQ 号、openid 或群号，也不要把凭据复制进沙箱。

<a id="deploy-with-nixos"></a>
## NixOS 部署

flake 提供软件包和 `nixosModules.default`。

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

会话工作区以 NixOS 容器（`systemd-nspawn`）运行，需要 `sandbox.enable = true`、`sandbox.vmRoot`、`systemd.network.enable = true` 和 `networking.nftables.enable = true`。容器根文件系统每次重启按模块重建，`/work` 保留。上线前请先读[容器沙箱](docs/container-sandbox.md)。

<a id="security"></a>
## 安全

- **密钥留在宿主。** QQ 凭据、模型密钥和运维凭据都不进沙箱，沙箱只拿到可撤销、限定会话的令牌。
- **沙箱碰不到你的网络。** 只放行公网、DNS 和中转端口，宿主和内网都访问不了。
- **人说了算。** 群主和管理员可以在自己的群里 `/stop`、`/start`、`/kill`；管理台能看会话、用量、任务和审计记录。
- **生成了不算送达。** 任务完成、文件上传、消息排队、平台回执，分开记录，各算各的。

详见[沙箱安全](docs/sandbox-security.md)。

<a id="documentation"></a>
## 文档

| 我想知道…… | 从这里看 |
| :--- | :--- |
| QQ 怎么接入、绑定和发送 | [QQ 官方接入](docs/qq-official.md) |
| 文件、媒体和主动接话怎么开 | [宿主配置](docs/qq-official-host-config.md) |
| 为什么沙箱访问不了宿主和内网 | [沙箱安全](docs/sandbox-security.md) |
| 会话工作区怎么运行 | [容器沙箱](docs/container-sandbox.md) |
| 为什么这样设计 | [架构决策记录](docs/adr/) |

<details>
<summary><strong>开发</strong></summary>

```sh
# 先给本地 PostgreSQL 测试容器设置 GAOJI_TEST_PASSWORD。
source tools/dev/testenv.sh gaoji_test
python -m unittest discover -s tests -p 'test_*.py'
ruff check bot.py src tests tools migrations nix
python tools/dev/check_unresolved_imports.py src
cd admin-ui && npm ci && npm run build
```

测试用单独的本地数据库，从不使用生产凭据。贡献前请先读 [AGENTS.md](AGENTS.md)：每个 PR 只做一件事，功能按真实行为验收，模拟回执不算送达。

</details>

## Star 趋势

<p align="center">
  <a href="https://star-history.com/#zty20040403/gaojibot&amp;Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=zty20040403/gaojibot&amp;type=Date&amp;theme=dark" />
      <img src="https://api.star-history.com/svg?repos=zty20040403/gaojibot&amp;type=Date" alt="Star 趋势" width="640" />
    </picture>
  </a>
</p>

<p align="center">
  如果 gaoji 让你的群聊变得更有意思，点个 ⭐ 能让更多人看到它。
</p>

---

<p align="center">
  <sub>Built by Kenneth · <a href="LICENSE">MIT 许可</a> · <a href="https://github.com/zty20040403/gaojibot/issues">问题与建议</a></sub>
</p>
