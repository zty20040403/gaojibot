import { useEffect, useState } from 'react';
import type { Installation } from './api';

const timestamp = (value?: number) => value && Number.isFinite(value) && value > 0 ? value * 1000 : undefined;
const duration = (seconds: number) => `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;

export function DeploymentProgress({ task, disconnected = false }: { task: Installation; disconnected?: boolean }) {
  const [now, setNow] = useState(Date.now());
  const active = ['queued', 'running', 'waiting'].includes(task.status);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active, task.job]);
  const start = timestamp(task.created_at);
  const updated = timestamp(task.updated_at);
  const end = active && !disconnected ? now : updated;
  const elapsed = start && end ? Math.max(0, Math.floor((end - start) / 1000)) : undefined;
  return <div className="deployment-progress">
    <div className="deployment-progress-summary"><span>任务历时：{elapsed === undefined ? '等待服务器记录' : duration(elapsed)}{disconnected ? '（断连前记录）' : ''}</span>
      <span>进度更新：{updated ? new Date(updated).toLocaleString() : '等待服务器回执'}</span></div>
    {task.progress && <div className="install-detail"><strong>{task.progress.detail}</strong><span>当前步骤已用 {duration(Math.max(0, Math.floor(task.progress.elapsed_seconds)))}</span>
      {task.progress.quiet_seconds >= 60 && <span>已有 {task.progress.quiet_seconds} 秒没有新输出，可能在等待下载源或编译</span>}</div>}
    <details><summary>任务详情</summary><dl><dt>任务编号</dt><dd><code>{task.job}</code></dd><dt>创建时间</dt><dd>{start ? new Date(start).toLocaleString() : '尚未确认'}</dd></dl></details>
  </div>;
}
