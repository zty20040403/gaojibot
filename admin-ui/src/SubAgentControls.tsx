import { useState } from 'react'
import { Save, RotateCcw, ChevronDown, ChevronUp, Square } from 'lucide-react'
import { StatusBadge, fmtTime, fmtNumber } from './components'
import type { useControlPlane } from './useControlPlane'
import { TaskOutcomePanel } from './TaskOutcomePanel'
import { TaskCollaborationPanel } from './TaskCollaborationPanel'

type Plane = ReturnType<typeof useControlPlane>

export function SubAgentControls({ detail, plane, models, roles }: { detail: any; plane: Plane; models: any[]; roles: any[] }) {
  const [policy, setPolicy] = useState<any>(detail.control?.policy ?? { mode: 'auto', roles: {} })
  const [baseVersion, setBaseVersion] = useState(Number(detail.control?.version ?? 0))
  const [target, setTarget] = useState('task')
  const [instruction, setInstruction] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showAll, setShowAll] = useState(false)
  const task = detail.task
  const collaboratorExecution = detail.collaboration && detail.collaboration.creator_user_id !== detail.collaboration.execution_user_id
  const current = target === 'task' ? policy : policy.roles?.[target] ?? { mode: policy.mode ?? 'auto', profile: policy.profile ?? '' }
  const frozen = ['running', 'planning', 'verifying', 'cancelling', 'revising', 'waiting_external'].includes(task.status)
  const editableSteps = (detail.runs ?? []).filter((r: any) => !r.step_key.startsWith('acceptance_r'))
  const modelEvents = (detail.events ?? []).filter((e: any) => e.event_type === 'agent.model_completed').reverse()

  function change(value: any) {
    setPolicy((old: any) => target === 'task' ? { ...old, ...value } : { ...old, roles: { ...old.roles, [target]: { ...current, ...value } } })
  }
  async function submit(kind: 'models' | 'revise') {
    setBusy(true); setError('')
    try {
      const payload = kind === 'models' ? { expected_version: baseVersion, policy }
        : { expected_version: baseVersion, instruction, step_keys: selected }
      const result = await plane.mutate('subagents', `/subagents/${task.task_id}/${kind}`, kind === 'models' ? 'PUT' : 'POST', payload, ['subagents', 'jobs', 'audit'])
      if (result) {
        setBaseVersion(baseVersion + 1)
        if (kind === 'revise') { setInstruction(''); setSelected([]) }
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : '修改失败') }
    finally { setBusy(false) }
  }
  return <section className="agent-controls">
    <div className="agent-controls-heading"><h3>任务控制</h3><span>修订 {detail.control?.revision ?? 1} · 配置版本 {detail.control?.version ?? 0}</span><StatusBadge value={detail.background ? 'background' : 'inline'} /></div>
    {task.status === 'waiting_external' && <button className="icon-button danger" title="取消等待与后续步骤；已提交的远程作业需另行核对" aria-label="取消等待中的任务" onClick={() => void plane.mutate('subagents', `/subagents/${task.task_id}/cancel`, 'POST', {}, ['subagents', 'jobs'])}><Square size={16} /></button>}
    {(detail.external_waits ?? []).slice(-5).map((item: any) => <div className="agent-delivery-row" key={`${item.run_id}:${item.call_id}`}><time>{fmtTime(item.updated_at)}</time><span title={item.remote_path}>agent#{item.run_id} · {item.remote_path || '等待提交回执'}</span><StatusBadge value={item.status === 'pending' ? 'waiting_external' : 'completed'} /></div>)}
    <div className="agent-model-controls">
      <label>范围<select aria-label="Agent 模型配置范围" value={target} onChange={e => setTarget(e.target.value)}><option value="task">任务默认</option>{roles.map(r => <option key={r.role} value={r.role}>{r.title}</option>)}</select></label>
      <label>模型策略<select aria-label="Agent 模型策略" disabled={frozen || busy || collaboratorExecution} value={collaboratorExecution ? 'auto' : current.mode ?? 'auto'} onChange={e => change({ mode: e.target.value, profile: e.target.value === 'auto' ? '' : current.profile || models[0]?.name || '' })}><option value="auto">自动匹配</option><option value="preferred">优先使用，允许降级</option><option value="locked">锁定，不允许降级</option></select></label>
      <label>模型<select aria-label="Agent 指定模型" disabled={frozen || busy || collaboratorExecution || current.mode === 'auto'} value={collaboratorExecution ? '' : current.profile ?? ''} onChange={e => change({ profile: e.target.value })}><option value="">按职责自动选择</option>{models.filter(m => target !== 'media' || m.vision).map(m => <option key={m.name} value={m.name}>{m.name}</option>)}</select></label>
      <button className="icon-button" title="保存模型策略" aria-label="保存模型策略" disabled={frozen || busy || collaboratorExecution} onClick={() => void submit('models')}><Save size={17} /></button>
      <button className="icon-button" title="重新载入已保存的策略" aria-label="重新载入已保存的策略" onClick={() => { setPolicy(detail.control?.policy ?? { mode: 'auto' }); setBaseVersion(detail.control?.version ?? 0) }}><RotateCcw size={17} /></button>
    </div>
    {detail.background && detail.revision_allowed !== false && !frozen && task.status !== 'queued' && <details className="agent-revision"><summary>追加修改或重做失败步骤</summary><div className="agent-step-selection">{editableSteps.map((run: any) => <label key={run.run_id}><input type="checkbox" checked={selected.includes(run.step_key)} onChange={e => setSelected(old => e.target.checked ? [...old, run.step_key] : old.filter(key => key !== run.step_key))} />{run.step_key}<StatusBadge value={run.status} /></label>)}</div><textarea aria-label="追加修改要求" placeholder="本次修改要求" value={instruction} onChange={e => setInstruction(e.target.value)} maxLength={12000} /><button disabled={busy || !selected.length || !instruction.trim()} onClick={() => void submit('revise')}>提交修订</button></details>}
    <TaskCollaborationPanel value={detail.collaboration} />
    {error && <div className="inline-error">{error}</div>}
    <div className="agent-result-states"><span>执行 <StatusBadge value={task.result?.execution_state ?? task.status} /></span><span>验收 <StatusBadge value={task.result?.validation?.acceptance?.status ?? 'pending'} /></span><span>文件投递 <StatusBadge value={task.result?.delivery_state ?? 'pending'} /></span></div>
    {detail.background && <div className="agent-result-states"><span>最终回复 <StatusBadge value={detail.final_delivery?.status ?? 'pending'} label={detail.final_delivery?.status === 'committed' ? '已送达' : detail.final_delivery?.status === 'ambiguous' ? '回执待核对' : detail.final_delivery?.status === 'failed' ? '投递失败' : '待投递'} /></span>{detail.final_delivery && <span>delivery#{detail.final_delivery.delivery_id} · {fmtTime(detail.final_delivery.updated_at)}</span>}</div>}
    <div className="table-scroll"><table className="agent-model-table"><thead><tr><th>完成时间</th><th>Agent</th><th>计划模型</th><th>实际模型</th><th>Token</th></tr></thead><tbody>{modelEvents.slice(0, showAll ? undefined : 5).map((e: any) => <tr key={e.event_id}><td>{fmtTime(e.created_at)}</td><td>agent#{e.run_id}</td><td>{e.payload.selected_profile}</td><td title={JSON.stringify(e.payload.routing)}>{e.payload.actual_profile || '未返回'}<small>{e.payload.actual_model}</small></td><td title={`输入 ${e.payload.input_tokens ?? 0} / 输出 ${e.payload.output_tokens ?? 0}`}>{fmtNumber(Number(e.payload.input_tokens ?? 0) + Number(e.payload.output_tokens ?? 0))}</td></tr>)}</tbody></table></div>
    {modelEvents.length > 5 && <button className="icon-button" aria-label={showAll ? '收起模型记录' : '展开模型记录'} title={showAll ? '收起模型记录' : '展开模型记录'} onClick={() => setShowAll(!showAll)}>{showAll ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</button>}
    {(detail.artifact_deliveries ?? []).slice(-5).reverse().map((item: any) => <div className="agent-delivery-row" key={`${item.revision}:${item.key}`}><time>{fmtTime(item.updated_at)}</time><span title={item.payload?.filename}>{item.payload?.filename}</span><StatusBadge value={item.state} /></div>)}
    <TaskOutcomePanel key={task.task_id} detail={detail} plane={plane} />
  </section>
}
