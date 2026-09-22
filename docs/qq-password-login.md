# QQ 密码登录恢复

这是可选的 NapCat 本机恢复工具，不是绕过腾讯账号验证的工具，也不能保证永不掉线。
默认关闭，不会替换扫码登录，不会启动已经手动停止的 NapCat。

## 行为

- QQ 已在线时核对真实账号，不提交密码、不退出其他账号。
- 正在扫码确认、初始化或重连时等待，不抢占已有登录流程。
- 明确未登录时尝试一次密码登录。只有实际在线且账号匹配才算成功。
- 验证码、新设备确认、明确风控提示、错误密码、请求超时或结果不明确时，停止自动提交。
- 调用前持久记录尝试，进程中断和重启不会重复提交。并发运行由文件锁互斥。
- 人工扫码成功后自动解除本次阻塞。也可以显式重新授权一次尝试；仍有 30 分钟间隔、24 小时最多 3 次的限制。
- 定时任务的 `active` 或退出码 0 只表示检查执行了，不代表 QQ 在线。以状态文件 `status=online` 和现有 QQ 健康指标为准。

## 密码保管

QQ 密码不是服务器 sudo 密码、WebUI Token 或模型 API Key，不要混用，也不要发到聊天中。
脚本初始化入口使用隐藏输入，文件仅所有者可读写。NixOS 用 `LoadCredential` 提供给服务，
不把密码写入 Nix store、进程参数、环境变量或 Git。API 要求的 MD5 同样属于敏感凭证。
脚本只访问 `127.0.0.1`，不使用代理、不跟随重定向，不输出原始登录响应或验证链接。
服务器 root 仍能读取凭证，不能把文件权限说成加密。

## 启用

先在服务器项目目录下，用可用的 Python 3 执行以下命令，交互录入一次：

```sh
sudo python3 nix/napcat-password-login.py --init-password /var/lib/gaoji-qq-login-secret/password
```

若服务器没有 `python3` 命令，使用实际安装的 Python 3 完整路径，不要把密码写入 shell 命令。
初始化拒绝覆盖已有文件。更新密码时用安全的本地秘密管理流程替换，保持私有目录和 `0600` 文件权限。

NixOS 配置仅引用这个路径（字符串，不能用 Nix 路径字面量或 `builtins.readFile`）：

```nix
services.gaoji.napcat.passwordLogin = {
  enable = true;
  passwordFile = "/var/lib/gaoji-qq-login-secret/password";
};
```

按项目既有发布流程更新机器人 input，再 fetch 并核对共享配置后 rebuild。
配置只应在 h610 的唯一生产 QQ 实例启用，不能让 h310 同号自动登录。

```sh
sudo systemctl start gaoji-qq-password-login.service
sudo journalctl -u gaoji-qq-password-login.service -n 10 --no-pager
sudo cat /var/lib/gaoji-qq-password-login/state.json
```

`captcha_required`、`device_confirmation_required` 或 `security_confirmation_required`：
去本机受保护的 NapCat WebUI 完成人工验证，不要循环重试。
`login_submitted_not_verified`：已提交，但还未确认在线，下一次检查会复核，不能宣布登录成功。
`manual_required`：查看 `blocked` 字段了解阻塞原因。

解决错误密码或网络问题后，明确允许下一次尝试（不会当场提交密码、不会清空次数限制）：

```sh
sudo gaoji-qq-password-login \
  --config /var/lib/napcat-chat-bot/config/webui.json --port 6100 \
  --uin 3580515978 --password-file /var/lib/gaoji-qq-login-secret/password \
  --state /var/lib/gaoji-qq-password-login/state.json --rearm
```

以上服务名和路径对应当前 h610；其他部署应替换为自己的配置。
关闭时将 `passwordLogin.enable` 设为 `false` 并 rebuild；临时停止可用
`sudo systemctl stop gaoji-qq-password-login.timer gaoji-qq-password-login.service`。

## 人工验证提醒

可以使用另一套已经在线的 QQ 实例发送定向提醒，高级自己掉线时仍可通知管理员。
配置 `passwordLogin.notification` 的 `enable`、`webuiConfigFile`、`webuiPort`、
`account`、`groupId`、`userId`；发送前核实通知账号和目标群成员，仅 @ 指定用户，不 @ 全体。
WebUI 凭据只在服务器本机读取，不能写入 Nix 配置或仓库。

设备确认、验证码、安全验证、账号不符或登录结果不明会使用不同的固定短提示。
通知不会代替腾讯的手机确认，不会把验证码、密码或带令牌的验证链接发到群里。
同一次阻塞只发送一次，记录持久化到同一状态文件的 `notification` 字段；
核实 QQ 恢复在线后清除本次提醒记录，下次新的阻塞可重新提醒。
通知账号离线、接口不可用或群成员无法核实时，每 5 分钟重新检查一次，期间不发消息。
发送前保存 `sending`，收到消息 ID 后才标记 `sent`；发送结果不明记为 `unconfirmed`，
不盲目重发。发送过程中崩溃留下的 `sending` 同样不自动重发，可查看服务日志人工核对。

## 验证范围

接口依据 NapCat v4.18.28 的 `QQLogin/PasswordLogin`。
本地测试使用模拟接口，覆盖验证码、设备确认、错误、恢复、防重复及凭证边界。
没有用户在服务器录入 QQ 密码前，不进行真实密码登录，也不能宣称已部署或已恢复在线。
