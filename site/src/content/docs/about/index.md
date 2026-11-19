---
title: 关于 gaoji
description: 开源仓库、技术方案、作者与致谢。
---

**gaoji** 是一个面向官方 QQ 机器人平台的多模型 AI 群友与任务 Agent。它的设计初衷很简单：**平时像懂分寸的老群友一样自然搭话、该安静时安静；遇到真要干活的事，能在隔离环境里把资料查实、把代码跑通、把文件做好交回群里。**

## 开源仓库与自部署

gaoji 采用 **MIT License** 完全开源，源码、架构文档与自部署说明均托管在 GitHub：

- **GitHub 仓库**：[zty20040403/gaojibot](https://github.com/zty20040403/gaojibot)
- **自部署与快速开始**：请直接阅读仓库首页 [README（简体中文）](https://github.com/zty20040403/gaojibot/blob/main/README.zh-CN.md) 与 [English README](https://github.com/zty20040403/gaojibot/blob/main/README.md)
- **问题反馈与建议**：[GitHub Issues](https://github.com/zty20040403/gaojibot/issues)

## 一句话技术栈

基于官方 QQ 机器人接口接入，后端使用 **Python 3.12** 与 **PostgreSQL 17（含 pgvector）** 持久化消息账本、分层记忆与任务状态，通过多模型网关驱动前台对话，将代码、文档与媒体长任务交给按会话隔离的 **Linux 沙盒** 异步执行，并以 **NixOS** 管理可复现部署。

本官网使用 **Astro + Starlight** 构建纯静态页面，源码位于同一仓库的 `site/` 目录。

## 作者与致谢

- **作者**：Kenneth
- **致谢**：
  - 感谢在日常群聊中陪伴测试、挑刺和提出无数真实场景建议的群友们；
  - 特别感谢兄弟项目 **Max**——在官方平台接入、精简架构边界与工程实践上提供了直接的参考与启发；
  - 感谢 Python、PostgreSQL、NixOS、Playwright、FFmpeg、Astro 与 Starlight 等开源社区维护者。

---

*声明：gaoji 为个人开源项目，非腾讯官方产品，与腾讯公司及 QQ 官方无关。*
