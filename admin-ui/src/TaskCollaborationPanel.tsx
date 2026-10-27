import { useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { fmtTime, StatusBadge } from './components'

type Feedback = {
  feedback_id: number; native_user_id: string; instruction: string; status: string
  base_revision: number; consumed_revision: number | null; created_at: number
}
type Snapshot = {
  creator_user_id: number; execution_user_id: number
  members: { principal_id: number; native_user_id: string; enabled: boolean }[]
  feedback: Feedback[]
}

export function TaskCollaborationPanel({ value }: { value?: Snapshot }) {
  const [expanded, setExpanded] = useState(false)
  if (!value) return null
  const members = value.members.filter(member => member.enabled)
  const pending = value.feedback.filter(item => item.status === 'pending').length
  return <div className="task-collaboration">
    <div className="agent-result-states">
      <span>发起人 <b>{value.creator_user_id}</b></span>
      <span>本版执行者 <b>{value.execution_user_id}</b></span>
      <span>参与者 {members.length}</span><span>待合并意见 {pending}</span>
    </div>
    {members.length > 0 && <div className="task-collaboration-members">{members.map(member =>
      <span key={member.principal_id}>{member.native_user_id}</span>)}</div>}
    {value.feedback.length > 0 && <ol className="task-feedback-list">{value.feedback.slice(0, expanded ? undefined : 5).map(item =>
      <li key={item.feedback_id}>
        <div className="task-feedback-meta"><span>f#{item.feedback_id} · {item.native_user_id}</span>
          <time>{fmtTime(item.created_at)}</time>
          <StatusBadge value={item.status} label={item.status === 'pending' ? '待合并' : item.status === 'consumed' ? `已合入第 ${item.consumed_revision} 版` : '已撤回'} />
        </div><p>{item.instruction}</p>
      </li>)}</ol>}
    {value.feedback.length > 5 && <button className="icon-button" aria-label={expanded ? '收起协作意见' : '展开协作意见'} title={expanded ? '收起协作意见' : '展开协作意见'} onClick={() => setExpanded(!expanded)}>
      {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
    </button>}
  </div>
}
