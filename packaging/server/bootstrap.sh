#!/bin/sh
set -eu
umask 077
export PATH=/run/current-system/sw/bin:/usr/sbin:/usr/bin:/sbin:/bin
job=$1
checksum=$2
case "$job" in ''|*[!a-f0-9]*) exit 2;; esac
case "$checksum" in ''|*[!a-f0-9]*) exit 2;; esac
test "${#job}" -eq 32
test "${#checksum}" -eq 64
test "$(id -u)" -eq 0
root=/var/lib/gaoji-app
work=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export HOME=/root
export LC_ALL=C
cd "$work"
status=$root/status/$job.json
bootstrap_failed() {
    printf '{"schema":1,"job":"%s","stage":"prepare","completed":[],"status":"failed","message":"服务器基础工具安装失败，请重新查询安装状态后重试"}\n' "$job" > "$status.tmp"
    chmod 644 "$status.tmp"
    mv "$status.tmp" "$status"
}
trap bootstrap_failed EXIT
if ! test -e "$root/jobs/$job.json"; then
    printf '{"schema":1,"job":"%s","stage":"prepare","completed":[],"status":"running","message":"正在准备服务器基础工具"}\n' "$job" > "$status.tmp"
    chmod 644 "$status.tmp"
    mv "$status.tmp" "$status"
fi
. /etc/os-release
case "$ID:$VERSION_ID" in
    nixos:*)
        if ! test -x "$root/tools/bin/python3"; then
            timeout 1800 nix-build --max-jobs 1 --cores 2 --out-link "$root/tools" "$work/environment.nix"
        fi
        python=$root/tools/bin/python3
        ;;
    debian:12|debian:13|ubuntu:22.04|ubuntu:24.04)
        if ! command -v python3 >/dev/null 2>&1; then
            export DEBIAN_FRONTEND=noninteractive
            apt-get -o Acquire::Retries=2 update
            apt-get install -y --no-install-recommends python3 ca-certificates
        fi
        python=$(command -v python3)
        ;;
    *) exit 2;;
esac
trap - EXIT
if test -n "${3:-}"; then
    exec "$python" "$work/installer.py" --job "$job" --archive "$work/gaoji-runtime.tar.gz" --sha256 "$checksum" --previous-sha256 "$3"
fi
exec "$python" "$work/installer.py" --job "$job" --archive "$work/gaoji-runtime.tar.gz" --sha256 "$checksum"
