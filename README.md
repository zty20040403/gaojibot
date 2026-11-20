<p align="right">
  <strong>English</strong> · <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="./docs/assets/readme/en/hero.gif" width="100%" alt="gaoji — chats like a friend, works like an agent. An open-source AI member for official QQ groups that answers @-mentions, stays quiet otherwise, runs background jobs and delivers with a platform receipt.">
</p>

<p align="center">
  <a href="https://github.com/zty20040403/gaojibot/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/zty20040403/gaojibot/ci.yml?branch=main&amp;style=flat-square&amp;label=CI&amp;labelColor=0B0D10&amp;color=5FD3B4" alt="CI status"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-12B7F5?style=flat-square&amp;logo=python&amp;logoColor=F4F4F0&amp;labelColor=0B0D10" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/PostgreSQL-17-12B7F5?style=flat-square&amp;logo=postgresql&amp;logoColor=F4F4F0&amp;labelColor=0B0D10" alt="PostgreSQL 17">
  <img src="https://img.shields.io/badge/Deploy-NixOS-12B7F5?style=flat-square&amp;logo=nixos&amp;logoColor=F4F4F0&amp;labelColor=0B0D10" alt="Deploy with NixOS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-FF7A59?style=flat-square&amp;labelColor=0B0D10" alt="MIT License"></a>
  <a href="https://github.com/zty20040403/gaojibot/stargazers"><img src="https://img.shields.io/github/stars/zty20040403/gaojibot?style=flat-square&amp;logo=github&amp;logoColor=F4F4F0&amp;label=Stars&amp;labelColor=0B0D10&amp;color=FFD166" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="#what-it-does">What it does</a> · <a href="#how-it-works">How it works</a> · <a href="#get-started">Get started</a> · <a href="#security">Security</a> · <a href="#documentation">Docs</a>
</p>

<a id="what-it-does"></a>
<p align="center">
  <img src="./docs/assets/readme/en/section-what.svg" width="100%" alt="01 · What it does">
</p>

<p align="center">
  <img src="./docs/assets/readme/en/capabilities.svg" width="100%" alt="Six capabilities: always answers @ and stays quiet otherwise; remembers the whole group; does real work in its own sandbox; sees, hears and draws; reminds and follows up; keys never leave the host.">
</p>

**gaoji** is an AI member for your QQ group. It answers whenever you @ it or DM it, keeps out of conversations that aren't for it, remembers the group, and hands long work — research, code, files, video — to a coding agent in that chat's own sandbox. You keep chatting while it works; the result comes back in the same voice.

| You say | gaoji does |
| :--- | :--- |
| *"Compare RAM prices this month, chart + CSV please."* | Says it has started → a background job searches and processes the data → posts the conclusion with sources, plus the files. |
| *"What is this video about?"* | Imports the attachment → samples frames and transcribes the audio in the sandbox → replies with a summary. |
| *"Also add the other model to that."* | Finds the running job and steers it instead of starting a second one. |
| *"Remind me about the meeting at 3 pm tomorrow."* | Creates a one-off reminder and delivers it on time. |

<sub>Examples of real flows, not screenshots. Results depend on the model, the host configuration and the permissions QQ grants.</sub>

**Why it's different**

|  | A typical chat bot | gaoji |
| :--- | :--- | :--- |
| **When it talks** | Replies to everything, or only to commands | Always answers @ and DMs; otherwise speaks only when it has something useful |
| **Memory** | The last few messages | Per-chat memory, full history search, a card for each regular |
| **Long tasks** | Blocks the chat or times out | Durable jobs you can check on, steer or cancel while you keep chatting |
| **Where code runs** | On the bot's host, or nowhere | In the chat's own NixOS container with a persistent `/work` |
| **Delivery** | Fire and forget | Durable outbox with real platform receipts |

<a id="how-it-works"></a>
<p align="center">
  <img src="./docs/assets/readme/en/section-how.svg" width="100%" alt="02 · How it works">
</p>

<p align="center">
  <img src="./docs/assets/readme/en/workflow.svg" width="100%" alt="A QQ message reaches the foreground agent, which replies through a durable outbox and records the platform receipt. Long tasks go to a background job inside the chat's own sandbox, which has no host keys and no LAN access.">
</p>

- **Foreground** — official QQ events → timeline and memory → multi-model agent → one durable send path.
- **Background** — durable jobs drive a coding agent in the chat's container; `/work` persists, and the report wakes the foreground to deliver it.
- **State** — PostgreSQL holds the message ledger, memory, jobs, schedules and delivery receipts.

<a id="get-started"></a>
<p align="center">
  <img src="./docs/assets/readme/en/section-start.svg" width="100%" alt="03 · Get started">
</p>

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 17 with pgvector, official QQ bot credentials and a model API. Files, browser and video need a Linux host with KVM.

```sh
git clone https://github.com/zty20040403/gaojibot.git
cd gaojibot
uv sync --locked
cp .env.example .env          # database, models, QQ credential file paths
uv run python -m src.bot_storage.cli upgrade
uv run python bot.py
```

| Configure | Where |
| :--- | :--- |
| Database, models, persona, search, sandbox, background jobs | [`.env.example`](.env.example) |
| Connecting and binding the official QQ bot | [Official QQ integration](docs/qq-official.md) |
| Files, media and proactive replies | [Host configuration](docs/qq-official-host-config.md) |

<details>
<summary><strong>Deploy with NixOS</strong></summary>

The flake ships the package and `nixosModules.default`.

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

Per-chat workspaces run as NixOS containers (`systemd-nspawn`) and need `sandbox.enable = true`, `sandbox.vmRoot`, `systemd.network.enable = true` and `networking.nftables.enable = true`. Container root filesystems are rebuilt on restart; `/work` persists. Read [Container sandbox](docs/container-sandbox.md) before going live.

</details>

<a id="security"></a>
<p align="center">
  <img src="./docs/assets/readme/en/section-security.svg" width="100%" alt="04 · Security">
</p>

- **Keys stay on the host.** QQ credentials, model keys and operations credentials never enter the sandbox; it only gets revocable, conversation-scoped tokens.
- **The sandbox can't reach your network.** Only the public internet, DNS and the relay ports — not the host, not the LAN.
- **People stay in charge.** Group owners and admins can `/stop`, `/start` and `/kill` the bot in their chat; the admin console shows chats, usage, jobs and audits.
- **Generated is not delivered.** A finished job, an uploaded file, a queued message and a platform receipt are tracked separately.

> [!IMPORTANT]
> The AppID, AppSecret and binding pepper are read from `0600` files on the host. Never commit secrets, real QQ numbers, openids or group numbers, and never copy credentials into the sandbox. More in [Sandbox security](docs/sandbox-security.md).

<a id="documentation"></a>
## Documentation

| I want to know… | Start here |
| :--- | :--- |
| How QQ is connected, bound and sent to | [Official QQ integration](docs/qq-official.md) |
| How files, media and proactive replies are enabled | [Host configuration](docs/qq-official-host-config.md) |
| Why the sandbox can't reach the host or LAN | [Sandbox security](docs/sandbox-security.md) |
| How per-chat workspaces run | [Container sandbox](docs/container-sandbox.md) |
| Why things are designed the way they are | [Architecture decisions](docs/adr/) |

<sub>Most in-depth documents are currently written in Chinese.</sub>

<details>
<summary><strong>Development</strong></summary>

```sh
# Set GAOJI_TEST_PASSWORD for the local PostgreSQL test container first.
source tools/dev/testenv.sh gaoji_test
python -m unittest discover -s tests -p 'test_*.py'
ruff check bot.py src tests tools migrations nix
python tools/dev/check_unresolved_imports.py src
cd admin-ui && npm ci && npm run build
```

Tests use a separate local database and never production credentials. Read [AGENTS.md](AGENTS.md) before contributing: one change per PR, and features are accepted on real behaviour — a simulated receipt is not a delivery.

</details>

<p align="center">
  <a href="https://star-history.com/#zty20040403/gaojibot&amp;Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=zty20040403/gaojibot&amp;type=Date&amp;theme=dark">
      <img src="https://api.star-history.com/svg?repos=zty20040403/gaojibot&amp;type=Date" alt="Star history" width="600">
    </picture>
  </a>
</p>

<p align="center">
  If gaoji made your group chat a little smarter, a ⭐ helps others find it.<br>
  <sub>Built by Kenneth · <a href="LICENSE">MIT License</a> · <a href="https://github.com/zty20040403/gaojibot/issues">Issues &amp; ideas</a></sub>
</p>
