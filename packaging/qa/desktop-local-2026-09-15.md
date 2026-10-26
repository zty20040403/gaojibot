# 本机部署 0.1.21

范围：这台 Apple Silicon Mac 的 Docker Desktop。未连接 h310、h610 或 tank，未登录真实 QQ，未调用付费模型。

最终运行包：`7d9bc06bbb1c5351b243d7803c3195c989bbbdf01ac6c4bb8fec7705f300bbfa`。

## 已验证

- 独立临时实例从安装入口构建本机 ARM 镜像，自动创建 PostgreSQL + pgvector、内部网络和 QQ 组件。
- 数据库迁移完成、数据库健康，NapCat 返回二维码；尚未配置模型的 Bot 没有启动。
- 管理组件重启后，实例编号和数据库不变，仍能获取配置状态。
- 非 root 运行镜像通过源码校验、依赖导入、音频工具、中文字体和离线 Chromium 渲染检查。
- Electron 真实 preload 和本机检测可用；1180/780 像素窗口没有横向溢出；可返回服务器连接页面。
- 4 项 Python 定向检查、2 项 Node 定向检查和 TypeScript 构建通过。没有运行全量测试。
- 临时实例的容器、网络、数据卷已按本次任务标签清理；原有 Docker 容器和数据卷不变。

首次运行发现并修复：ARM 音频依赖缺少 C++ 编译环境、轻量控制镜像漏装本地共享模块、新增 Docker 构建配方未列入运行镜像源码校验的构建文件集合。编译工具安装和移除在同一镜像层完成，不永久留在运行镜像中。

## 限制

- Windows 打包和路径适配完成，不代表 Windows/WSL2 已实机运行。
- 真实 QQ 扫码、模型回复、群文件交付需用户自己的账号验收。
- 本机版暂不开放高级沙盒、跨版本升级/回退、备份迁移；原有 Linux 服务器模式保留这些功能。
- Docker Desktop 及系统虚拟化需要用户安装/确认，不静默接受第三方许可或修改系统安全配置。

原始脱敏回执：`packaging/build/desktop-local-check.json`。本机开发验证入口为 `desktop/scripts/verify-local-deployment.cjs`，必须只对本机 Docker Desktop 使用。
