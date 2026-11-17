# NixOS 容器沙盒：切换与回退手册

`services.gaoji.sandbox.backend = "container"` 把每个会话的工作区从 Debian 虚拟机换成 NixOS 容器（systemd-nspawn）。默认仍是 `vm`。

## 结构

- **容器系统**：`nixosConfigurations.gaoji-container`（`nix/guest/container.nix`、`nix/guest/tools.nix`）。所有容器共用，工具来自宿主只读的 `/nix/store`。部署一次，下次启动的容器就是新版本。
- **每个会话一个容器**：`gaoji-ct@<编号>.service`（运维会话是 `gaoji-ct-ops@`）。系统目录每次启动重建，只有 `/var/lib/gaoji-ct/<编号>/work` 保留，在容器里是 `/workspace` 和 `/work`。用户命名空间隔离，`/workspace` 用 idmap 挂载，容器里的 `sandbox`（1000）拥有它。内存、CPU、任务数按 `sandbox.memoryMiB`、`cpus`、`pidsLimit` 限制。
- **代理**：`gaoji-ct-broker`（root）是机器人唯一的特权入口，只做启动、停止、状态、在容器里执行命令（`systemd-run --machine`）、删除已停止容器的数据。套接字 `/run/gaoji-ct/broker.sock` 只有机器人的用户能连。
- **网络**：网桥 `vz-gaoji`，宿主地址是 `10.0.2.2`（和虚拟机里一样，所以 `gaoji` 命令、Codex、pi 的地址不变）。端口隔离，容器之间互相看不见；nftables 只放行公网、DHCP 和中继端口 18640/18641。运维会话的容器加入 `gaoji-ops` 命名空间，可以 `ssh <主机>`。
- **元数据**：仍在 `sandbox.vmRoot`，沙盒编号不变，所以同一会话切换前后用同一个编号。

## 切换（owner 操作）

1. 在 nix-config 里设 `sandbox.backend = "container";` 并确认 `systemd.network.enable` 和 `networking.nftables.enable` 已开启，构建并检查闭包。
2. 停掉 gaoji 和所有旧虚拟机：`systemctl stop gaoji`，再按 `docs/` 里的老办法确认 `virsh list` 为空。
3. 预演迁移：`tools/migrate_vm_workspaces.sh /data/services/gaoji/vms`，确认列出的会话和大小。
4. 正式迁移：同一命令加 `--apply`。旧磁盘只读，不会改动。
5. 切换系统，检查 `systemctl status gaoji gaoji-ct-broker.socket`、`/health/qq-official`。
6. 在测试群里验收：
   - 让后台做一个 PDF 并发出来；
   - 发一个大于 10 MiB 的文件（旧 QGA 上限）；
   - 在运维会话里 `ssh tank uptime`；
   - 确认 `/workspace` 里迁过来的旧文件还在。

## 回退

把 `sandbox.backend` 改回 `vm` 并切换。旧虚拟机磁盘没被动过，容器里新写的文件不会回到旧磁盘。

## 已知限制

- 容器和宿主共用内核，隔离比虚拟机弱。
- `apt` 装的软件、旧虚拟机 `/root` 下的文件不迁移。容器里不能用 `apt`；缺的 Python/Node 包用 `pip`/`npm`/`uv` 装到 `/workspace`。
- 容器里没有 Nix 守护进程，`nix shell` 不可用。
