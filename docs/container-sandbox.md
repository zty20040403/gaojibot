# NixOS 容器沙盒说明

每个会话的工作区以无状态 NixOS 容器（`systemd-nspawn`）运行，`services.gaoji.sandbox.backend` 固定为 `"container"`。

## 结构

- **容器系统**：`nixosConfigurations.gaoji-container`（`nix/guest/container.nix`、`nix/guest/tools.nix`）。所有容器共用同一系统闭包，工具来自宿主只读挂载的 `/nix/store`。宿主部署新版本后，容器下次启动即使用新工具链。
- **每个会话一个容器**：`gaoji-ct@<编号>.service`（运维会话是 `gaoji-ct-ops@`）。系统根目录每次启动重建，只有 `/var/lib/gaoji-ct/<编号>/work` 保留，在容器里挂载为 `/workspace` 和 `/work`。启用用户命名空间隔离，`/workspace` 用 idmap 挂载，容器里的 `sandbox`（1000）拥有它。内存、CPU、任务数按 `sandbox.memoryMiB`、`cpus`、`pidsLimit` 限制。
- **代理**：`gaoji-ct-broker`（root）是机器人唯一的特权入口，只做启动、停止、状态查询、在容器里执行命令（`systemd-run --machine`）、删除已停止容器的数据。套接字 `/run/gaoji-ct/broker.sock` 只有机器人的运行用户可连。
- **网络**：每个容器有自己的网络命名空间，`127.0.0.1:9222` 只连到本会话的浏览器。普通容器接 `vz-gaoji`，只放行公网、DHCP 和宿主中继 `10.0.2.2:18640/18641`。运维容器接 `vz-gaoji-ops`，也使用 DHCP 和相同的宿主中继。两个桥都禁止容器端口互通。
- **运维路由**：默认运维网段是 `10.237.0.16/28`，网关 `.17`。宿主 networkd 将来自运维桥的流量送入表 237，经 `gaoji-uplink` 到 `gaoji-ops` 路由命名空间。它通过 `tailscale0` 访问机群，通过原有上联访问公网；容器共享一个 `gaojiops` 出口身份，不共享浏览器。MagicDNS 和 `ssh <主机>` 的客体配置不变。若运维会话还要访问宿主自身，请将 `services.gaoji.sandbox.operations.hostAddress` 设为宿主 `tailscale ip -4` 的值。这样连接宿主也经过 `gaojiops` 和 tailnet ACL，而不是从桥上直连宿主服务；默认 `null` 不启用。
- **元数据**：保存在 `sandbox.vmRoot`（每个沙盒一个 `<编号>/metadata.json`）及宿主文件锁中。

## 宿主 `/work` 快照（可选）

在已启用容器沙盒的宿主上，管理员先确认本机备份卷已挂载、备份父目录可信且路径不含符号链接，再以 root 执行 `install -d -m 0700 -o root -g root /data/services/gaoji/backups/work`。设置 `services.gaoji.sandbox.workSnapshots.enable = true;` 并按正常流程应用宿主配置。`systemctl list-timers gaoji-work-snapshot.timer` 查看下次运行；`systemctl start gaoji-work-snapshot.service` 可立即执行，`journalctl -u gaoji-work-snapshot.service` 查看结果。无需进入、启动或停止容器。

定时器每天 03:00 UTC 执行，关机错过则补跑一次。仅从 `/var/lib/gaoji-ct/s<六位小写十六进制>/work` 读取，不读同级容器根、元数据、数据库或 bot 环境文件。每个 scope 每个 UTC 日最多一份 `编号_YYYY-MM-DD.tar`（root:root，0600），运行时清理超过最近 30 个 UTC 日期的归档，包括已删除的 scope。同日重跑仅在该 scope 写完后原子替换；一个 scope 报错不妨碍其他 scope 发布，失败不改变仍在保留期内的旧归档。

这是在线文件副本，不是应用级一致性快照或异机容灾。普通文件、目录和符号链接入包；链接只存目标文本，不打开目标。跳过设备、FIFO、socket，不保留 uid/gid、ACL、xattr 或特权位。普通文件的硬链接若全部位于同一 scope 的 `/work` 内，每个名字都按普通文件入包；链接计数不完整、遇到子挂载（包括同设备 bind mount）或检测到读取中的改动，该 scope 报错而不发布。大工作区请预留未压缩 tar 和单份临时文件的空间。

**恢复演练**：在宿主上选中一个 scope 和日期，以有权读取归档的管理员运行下列命令。只解到全新私有目录，不覆盖线上 `/work`。默认不还原符号链接，避免后续工具意外读取链接指向的其他位置；确有需要时可从归档清单人工核对链接目标后再建立。

```sh
archive=/data/services/gaoji/backups/work/s000001_2026-01-01.tar  # 替换为实际文件
restore=$(mktemp -d)
python3 - "$archive" "$restore" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2], members=[m for m in archive if m.isfile() or m.isdir()], filter='data')
PY
ls -la -- "$restore"
```

## 已知限制

- 容器和宿主共用内核，隔离边界弱于独立内核虚拟机；宿主 `/nix/store` 对容器只读可见，因此不能把密钥写进 Nix store。
- 容器里不能使用 `apt`，也没有 Nix 守护进程（`nix shell` 不可用）；缺少的 Python/Node 包用 `uv`/`pip`/`npm` 装到 `/workspace`。
