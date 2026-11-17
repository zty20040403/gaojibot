# NixOS 容器沙盒说明

每个会话的工作区以无状态 NixOS 容器（`systemd-nspawn`）运行，`services.gaoji.sandbox.backend` 固定为 `"container"`。

## 结构

- **容器系统**：`nixosConfigurations.gaoji-container`（`nix/guest/container.nix`、`nix/guest/tools.nix`）。所有容器共用同一系统闭包，工具来自宿主只读挂载的 `/nix/store`。宿主部署新版本后，容器下次启动即使用新工具链。
- **每个会话一个容器**：`gaoji-ct@<编号>.service`（运维会话是 `gaoji-ct-ops@`）。系统根目录每次启动重建，只有 `/var/lib/gaoji-ct/<编号>/work` 保留，在容器里挂载为 `/workspace` 和 `/work`。启用用户命名空间隔离，`/workspace` 用 idmap 挂载，容器里的 `sandbox`（1000）拥有它。内存、CPU、任务数按 `sandbox.memoryMiB`、`cpus`、`pidsLimit` 限制。
- **代理**：`gaoji-ct-broker`（root）是机器人唯一的特权入口，只做启动、停止、状态查询、在容器里执行命令（`systemd-run --machine`）、删除已停止容器的数据。套接字 `/run/gaoji-ct/broker.sock` 只有机器人的运行用户可连。
- **网络**：网桥 `vz-gaoji`，宿主地址是 `10.0.2.2`。网桥端口隔离，容器之间互相不可见；nftables 只放行公网、DHCP 和宿主中继端口 18640/18641。运维会话的容器加入 `gaoji-ops` 网络命名空间，可通过独立 Tailscale 节点执行 `ssh <主机>`。
- **元数据**：保存在 `sandbox.vmRoot`（每个沙盒一个 `<编号>/metadata.json`）及宿主文件锁中。

## 已知限制

- 容器和宿主共用内核，隔离边界弱于独立内核虚拟机；宿主 `/nix/store` 对容器只读可见，因此不能把密钥写进 Nix store。
- 容器里不能使用 `apt`，也没有 Nix 守护进程（`nix shell` 不可用）；缺少的 Python/Node 包用 `uv`/`pip`/`npm` 装到 `/workspace`。
