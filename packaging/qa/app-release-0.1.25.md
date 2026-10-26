# 高级 App 0.1.25 交付核对

日期：2026-09-16。范围：本地实现、Mac / Windows 安装包与更新分发。
不连接或部署 h310；用户自行通过 App 做服务器验收。

## 可交付产物

- [公开发布页](https://github.com/zty20040403/gaoji-app/releases/tag/app-v0.1.25)
- Mac Apple Silicon：`Gaoji-0.1.25-arm64.dmg`、`Gaoji-0.1.25-arm64-mac.zip`
- Windows x64：`Gaoji-Setup-0.1.25.exe`、`Gaoji-0.1.25-win.zip`
- 更新与校验：`app-update.json`、`latest.yml`、`release.json`、`SHA256SUMS`
- 用户说明：[首次使用与服务器试用步骤](../../desktop/QUICKSTART.zh-CN.md)

客户端版本 `0.1.25`，随附运行代码版本 `0.20.0`，共 280 个清单文件。
两端运行归档 SHA-256 相同：

```text
22d3d1bad545e5304b8b6b320cef261d3aa01232c6130a2b239a1c6756865d9a
```

## 本版直接证据

- `verify-package.cjs` 已重新检查两端产物：必需文件、归档校验、关键源码一致性和排除列表通过；Windows 主程序确认为 x64 PE。此检查不执行 Windows 程序。
- 发布流程核对八个最终文件的大小和 GitHub SHA-256 后才公开。分段上传只作传输暂存，合并工作流成功后已删除所有分段；公开版本没有暂存分段。
- [GitHub 合并校验任务](https://github.com/zty20040403/gaoji-app/actions/runs/35073864674) 成功，未向 GitHub 上传用户配置、会话或秘密。
- 最终打包 Mac App 在隔离临时用户目录中实际启动。更新窗口真实请求公开仓库，显示 `0.1.25` 和“当前已是最新发布版”；截图为 `desktop/release/qa/app-update-public-0.1.25.png`。
- Mac 更新窗口交互、保留未保存表单及宽窄窗口检查通过；8 项更新模块定向测试通过。这不等于服务器安装或 QQ 交付通过。
- Mac 安装包的匿名下载实际返回 HTTP 206 和请求的前 1024 字节。
- Windows 安装包的匿名下载也返回 HTTP 206；读取的 1024 字节含 `MZ` 可执行文件头，不是登录页或错误页。这不是 Windows 启动测试。
- 包内明确包含 Ledger、Context Pipeline、Historian、长期记忆与语义召回代码；没有携带生产聊天数据库。Historian 和语义召回默认关闭，后者需要另配 Embedding 服务。

## 按方案核对

| 方案要求 | 实现与证据位置 | 当前结论及限制 |
| --- | --- | --- |
| SSH、身份确认、登录秘密保护 | `desktop/lib/ssh.cjs`、`connections.cjs`、`preflight.cjs`；历版真实 localhost SSH 与 Electron 记录 | 已实现；不把 localhost 证据称作本版 h310 验收 |
| 完整独立运行包 | `packaging/build-runtime.mjs`、两端打包核对、运行归档清单 | 当前包已核对；不含用户数据库、QQ 会话或私人配置 |
| Debian / Ubuntu 自动安装 | `packaging/server/installer.py`；`fresh-install-2026-09-15.md` | 历版干净 Ubuntu 到扫码入口有实测；不是本版所有发行版均已验证 |
| NixOS 隔离安装 | 同一安装器的 NixOS 适配、`desktop/lib/preflight.cjs` | 有实现；当前未证明本版 h310 从安装到收发完整成功 |
| 持久安装、断线接续 | `desktop/lib/deploy.cjs`、安装器持久状态；Ubuntu 历史验收 | 有本地历史证据；不能承诺任意中断均已覆盖 |
| 独立数据库、备份与恢复 | `src/local_runtime/runtime.py`、`backups.py`、`maintenance.py`；迁移与删除 QA | 同版本隔离 Ubuntu 验收已有记录；跨版本和真实 QQ 会话迁移仍未全部覆盖 |
| 扫码、模型、群配置 | `SetupPanel.tsx`、`ModelCatalog.tsx`、`setup_clients.py` | 入口与协议实现已交付；真实账号登录、模型回复需用户验证 |
| 原控制台与功能组件 | 原 `admin-ui` 随包构建，`ComponentPanel.tsx` 与运行配置映射 | 代码保留；外部集群、桥接、Embedding 需要用户自己的服务，不能当成开箱已连通 |
| 文件交付、恢复、容量、更新与卸载 | 对应生命周期模块、存储与更新面板；已有分项 QA | 分项证据存在；当前 QQ 群文件收件、跨版本及更多故障组合未全验收 |
| 双平台安装包与使用说明 | 本页、公开 Release、`QUICKSTART.zh-CN.md` | 两端包已交付；Mac 实际启动通过，Windows 实机仍未执行 |
| 后续增加的本机部署 | `desktop/lib/local.cjs`、`LocalPanel.tsx`、`desktop-local-2026-09-15.md` | Apple Silicon 到扫码入口有历史实测；Windows / WSL2 未实测，本机高级沙盒与迁移仍有限制 |

## 尚不能宣布完整 V1 完成

1. Windows 实机安装、启动、SSH 部署及 Windows / WSL2 本机部署没有直接运行证据。
2. 本版 NixOS / h310 与其他目标发行版的全流程不能由旧 Ubuntu 检查代替。
3. 真实 QQ 登录、模型回复、群文件收件及外部桥接需要用户自己的账号与环境。
4. 完整跨版本与故障恢复、可选组件的真实端到端验收仍未齐全。
5. 两端没有发行签名或 Mac 公证，当前明确作为预览版提供；不能伪称已签名。
6. 当前镜像目录仍为空，首次安装可能从源码构建。镜像选择和校验支持已实现，不等于预构建镜像已经发布。

本次只是交付与验收范围核对，不新增远程部署授权。不能凭公开发布、绿色构建或代码存在把上述缺口标为通过。
