# 会话工作区
Summary: 通过后台 agent 在本群持久 Linux VM 中制作文件、执行代码和分析附件。

1. 前台先用 `say` 说清开始做什么，再调用 `agent`，把目标、输入和交付文件名写清楚。前台可以继续聊天；追加要求用 `agent_steer`。
2. 用户附件先用 `import_file_to_sandbox` 导入，把返回的 `/work/chat/…` 路径交给后台。不要向用户索取已经提供过的文件。
3. 后台是本会话 VM 里的 Codex：以 root 跑命令、改文件、装软件，用 `view_image` 看图。文件放 `/work`，任务结束后保留。VM 里没有宿主凭据。
4. 浏览器和转写已装在 `gaoji-python` 环境里；Python 包可装在工作区自己的 venv。
5. 中文 PDF 用 `gaoji-pdf input.md output.pdf`。交付前用 `pdftotext` 核对内容、`pdffonts` 核对嵌入字体；需要看版式时转成 PNG 再看。
6. 网页正文用 `webread URL`，长文加 `--offset`；它也能读在线 PDF。需执行脚本的网页用 `gaoji-python` 调 Playwright：`from gaoji_browser import page, snapshot`。
7. 视频用 ffmpeg 抽帧，`gaoji-transcribe /work/chat/文件 --language zh` 转写，再综合画面和音轨。不要只凭文件名猜内容。
8. 后台把结果和文件绝对路径写入报告，不直接发群消息。前台收到完成结果后用 `send_file_from_sandbox` 交付，核对返回的 `message_accepted` 再告诉用户已发送。失败就报告实际错误。
