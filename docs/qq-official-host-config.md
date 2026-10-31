# 官方 QQ 宿主配置

`AI_QQ_OFFICIAL_HOST_CONFIG_FILE` 指向服务用户可读的私有 JSON 文件。NixOS 用 `services.gaoji.qqOfficial.hostConfigFile` 加载为 systemd credential。不要放密钥值、Python 导入路径或未经核实的身份映射。

下面使用测试身份展示字段。正式 `account` 必须由真实 AppID 与环境组成，文件目录须位于服务允许写入的私有状态目录。

```json
{
  "version": 1,
  "account": "account:v1:[\"qq-official\",\"TEST_APP_ID\",\"production\"]",
  "files": {
    "private_directory": "/var/lib/gaoji/state/qq-official-files",
    "generation_enabled": true,
    "max_file_bytes": 104857600
  },
  "media": {
    "ocr": true,
    "asr": true,
    "local_asr": false,
    "documents": true,
    "forwards": true,
    "stickers": true
  },
  "native_background": {
    "proactive": {
      "enabled": false
    }
  }
}
```

文件通过当前官方账号上传，不再配置 `native_routes` 或上传证明。实际大小还受 VM 文件导出上限和平台返回限制约束；宿主和 VM 的上限应保持一致。`generation_enabled` 需要工作区后端可用。

媒体能力按上面的开关启用。OCR/图片理解需要有效的视觉 profile；短语音优先读取腾讯识别文本，`local_asr` 需要宿主 whisper.cpp 与模型。视频和大附件先导入 VM，由后台处理。语音回复复用文件服务生成和发送 SILK。

主动接话启用时提供 `profile`，可指定 `max_reply_chars`。新消息去抖三秒，每个会话每小时最多八次；旧 `percent`、`threshold` 和 `max_checks_per_hour` 字段应删除。定时任务由 `schedules` 表和工具管理，不放在此文件。

旧的项目授权、迁移路线、Wiki、主机写操作策略已移除。管理员后台只读运维另用 `AI_OPS_READ_BASE_URL` 和 `AI_OPS_READ_TOKEN_FILE`；token 只在宿主读取。
