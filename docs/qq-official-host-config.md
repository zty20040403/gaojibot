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

图片说明入库另由管理台“媒体审核 → 图片说明”按会话启用，默认关闭、每日 200 张（北京时间），可调为 0–10,000 张。宿主须启用 `AI_MEDIA_ENABLED=true` 并配置支持视觉的 `AI_VISION_PROFILE`。启用后仅处理新入站图片/表情，不补旧图；说明可在 `history` 搜到。失败、超额和费用可查；关闭不退款已经发生的模型调用。普通图片不会自动加入全局可发表情库。

媒体能力按上面的开关启用。`ocr` 开关现在启用 `view_image` 和前台图片输入：前台 profile 的 `capabilities.vision=true` 时直接附当前/合并/引用图片（最多四张），否则用有效的视觉 profile 返回画面与文字。原图下载和模型图均受 `AI_VISION_MAX_IMAGE_MB`（默认 20 MiB，下载硬上限 100 MiB）限制；宿主须有 ffmpeg（Nix 包与开发 shell 已包含），缩到长边 1568，解码失败明确报错、不发送原始坏图。旧 OCR 描述回退仍沿用 `max_download_bytes` 上限和 `official_ocr` 用量分类；短语音优先读取腾讯识别文本，`local_asr` 需要宿主 whisper.cpp 与模型。视频和大附件先导入 VM，由后台处理。语音回复复用文件服务生成和发送 SILK。

视频直传可选：在宿主环境设置 `AI_VIDEO_PROFILE=<模型配置名>`（默认空），该 profile 必须为 `openai-chat`、显式 `capabilities.video=true`，且服务实际接受 Chat 的 `video_url` 数据 URL。建议 `max_input_tokens=65536`、`max_output_tokens=1024`、`timeout_seconds=180`；输入窗口至少覆盖每秒 100 token 加 1024 的余量。密钥只留宿主，不写入 VM。此约定不能等同于任意 OpenAI-compatible 服务都支持视频，需 owner 实测。

VM 需通过模板或显式工具升级安装 `gaoji-video`，将 `/work` 下视频转换为最长 10 分钟、1fps、长边 640、单声道、最大 32 MiB 的 MP4；超过时长先拒绝，不偷偷截断。导入结果的 `media_handle` 和路径一起交给后台。配置视频模型后后台有 `analyze_video`；未配置时用 ffmpeg 抽帧、`view_image`、`gaoji-transcribe`，并用 `record_video_summary` 保存结论。两者都写可搜索的会话历史说明；费用按原会话额度控制并关联后台任务。模型错误明确失败，不宣称已看完；未知费用保守保留。

主动接话启用时提供 `profile`，可指定 `max_reply_chars`。新消息去抖三秒，每个会话每小时最多八次；旧 `percent`、`threshold` 和 `max_checks_per_hour` 字段应删除。定时任务由 `schedules` 表和工具管理，不放在此文件。

旧的项目授权、迁移路线、Wiki、主机写操作策略已移除。管理员后台只读运维另用 `AI_OPS_READ_BASE_URL` 和 `AI_OPS_READ_TOKEN_FILE`；token 只在宿主读取。
