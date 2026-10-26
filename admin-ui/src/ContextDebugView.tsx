import { useEffect, useMemo, useRef, useState } from 'react'
import {
  BrainCircuit,
  Check,
  CircleHelp,
  MessageSquareText,
  ThumbsDown,
  ThumbsUp,
  UserRound,
  UsersRound,
} from 'lucide-react'
import {
  DataTable,
  EmptyState,
  Metric,
  PageHeader,
  RefreshButton,
  Section,
  StatusBadge,
  fmtNumber,
  fmtTime,
} from './components'
import type { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>

function ReactionVotes({ votes }: { votes: any }) {
  return <span className="reaction-votes">
    <span title="提问者自己的评价">提问者：{votes?.asker === 1 ? '赞同' : votes?.asker === -1 ? '不认可' : '未评价'}</span>
    <span title="其他群友的赞同票"><ThumbsUp size={14} />{fmtNumber(votes?.others_up)}</span>
    <span title="其他群友的不认可票"><ThumbsDown size={14} />{fmtNumber(votes?.others_down)}</span>
  </span>
}

const SOURCE_LABELS: Record<string, string> = {
  relation_graph: '引用与话题关系',
  group_timeline: '最近群聊',
  raw_history: '历史原消息',
  group_memory: '群公共记忆',
  user_memory: '当前用户记忆',
  historian_episode: 'Historian 章节',
  pinned_message: '固定消息',
  shared_source: '帖子或视频',
  media_summary: '媒体简介',
  reference_resolver: '引用解析器',
  audit_summary: '审计容量提示',
}

const ROUTE_LABELS: Record<string, string> = {
  chronological_projection: '时间顺序上下文',
  direct: '明确引用',
  follow_up: '追问关联',
  recent_group: '最近群聊',
  old_topic: '旧话题召回',
  user_memory: '个人记忆',
  group_memory: '群记忆',
  no_recall: '独立问题',
}

const REASON_LABELS: Record<string, string> = {
  selected: '综合得分通过，已放进上下文',
  source_threshold_pass: '达到该来源最低分',
  relative_threshold_pass: '接近本轮最高分',
  below_source_threshold: '低于该来源最低分',
  below_relative_threshold: '与本轮最佳候选差距太大',
  redundancy_penalty: '和已选内容重复太多',
  source_limit_reached: '同类证据已经足够',
  global_limit_reached: '本轮上下文名额已满',
  merged_duplicate: '与另一条候选重复，已合并',
  leakage_risk: '可能串群或串用户，已硬隔离',
  scope_rejected: '不属于当前群或当前用户，已硬隔离',
  empty_content: '内容为空',
  selected_by_reference_resolver: '引用解析器选中',
  not_selected_by_reference_resolver: '引用解析器未选中',
  audit_truncated: '候选过多，其余低分记录已省略',
  included_in_final_prompt: '实际送入模型',
}

function rows(value: unknown): any[] {
  return Array.isArray(value) ? value : []
}

function percent(value: unknown): string {
  return `${Math.round(Math.max(Number(value ?? 0), 0) * 100)}%`
}

export function ContextDebugView({ plane }: { plane: Plane }) {
  const payload = plane.data.contextDebug ?? {}
  const [negativeOnly, setNegativeOnly] = useState(false)
  const [negativeItems, setNegativeItems] = useState<any[]>([])
  const [filterError, setFilterError] = useState('')
  const items = negativeOnly ? negativeItems : rows(payload.items)
  const historian = payload.historian ?? {}
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [detailHistorian, setDetailHistorian] = useState<any>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [note, setNote] = useState('')
  const noteDraft = useRef({ turnId: null as number | null, dirty: false, revision: 0 })

  useEffect(() => {
    if (!negativeOnly) return
    const controller = new AbortController()
    void plane.query('/context-debug?negative_only=true&limit=100', controller.signal)
      .then((result) => { if (!controller.signal.aborted) { setNegativeItems(rows(result.items)); setFilterError('') } })
      .catch((reason) => { if (!controller.signal.aborted) setFilterError(String(reason)) })
    return () => controller.abort()
  }, [negativeOnly, plane.query, plane.versions['context-debug'], plane.data.contextDebug])

  useEffect(() => {
    if (!items.length) {
      setSelectedTurn(null)
      return
    }
    if (selectedTurn === null || !items.some((item) => Number(item.turn_id) === selectedTurn)) {
      setSelectedTurn(Number(items[0].turn_id))
    }
  }, [items, selectedTurn])

  useEffect(() => {
    if (selectedTurn === null) {
      setDetail(null)
      return
    }
    const controller = new AbortController()
    if (noteDraft.current.turnId !== selectedTurn) {
      noteDraft.current = { turnId: selectedTurn, dirty: false, revision: 0 }
      setNote('')
      setDetail(null)
    }
    setLoadingDetail(true)
    void plane.query(`/context-debug/${selectedTurn}`, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setDetail(result.item ?? null)
        setDetailHistorian(result.historian ?? null)
        if (!noteDraft.current.dirty) setNote(String(result.item?.feedback?.note ?? ''))
        setDetailError('')
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setDetailError(reason instanceof Error ? reason.message : '上下文详情加载失败')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingDetail(false)
      })
    return () => controller.abort()
  }, [plane.query, plane.versions['context-debug'], plane.data.contextDebug, selectedTurn])

  const tokenUsage = detail?.token_usage ?? {}
  const tokenTotal = Number(tokenUsage.total ?? 0)
  const tokenSegments = useMemo(() => [
    ['当前问题与引用', 'focus', '#2563eb'],
    ['当前群时间线', 'timeline', '#16a34a'],
    ['旧消息召回', 'semantic', '#d97706'],
    ['群记忆', 'group_memory', '#7c3aed'],
    ['个人记忆', 'user_memory', '#db2777'],
  ].map(([label, key, color]) => ({ label, key, color, value: Number(tokenUsage[key] ?? 0) })), [tokenUsage])

  const setFeedback = async (verdict: 'correct' | 'off_topic') => {
    if (selectedTurn === null) return
    const revision = noteDraft.current.revision
    try {
      await plane.mutate(
        'context-debug',
        `/context-debug/${selectedTurn}/feedback`,
        'PUT',
        { verdict, note },
        ['contextDebug', 'contextPlans'],
      )
      if (noteDraft.current.turnId === selectedTurn && noteDraft.current.revision === revision) noteDraft.current.dirty = false
    } catch {
      // The control plane displays the failure; keep the draft for a retry.
    }
  }

  return (
    <>
      <PageHeader
        title="上下文调试"
        description="逐条查看机器人把哪段聊天当成当前话题、为什么选中，以及哪里可能答非所问"
        action={<RefreshButton loading={plane.loading.has('contextDebug')} onClick={() => void plane.refresh('contextDebug')} />}
      />
      <div className="metric-grid compact context-debug-metrics">
        <Metric label="待整理章节" value={fmtNumber(historian.backlog)} hint={`${fmtNumber(historian.running)} 个正在整理`} />
        <Metric label="等待重试" value={fmtNumber(historian.retrying)} hint="模型失败后会自动再试" />
        <Metric label="最终失败" value={fmtNumber(historian.failed)} hint="需要管理员查看错误" />
        <Metric label="已记录回答" value={fmtNumber(items.length)} hint="当前加载的最近记录" />
      </div>

      <div className="context-debug-layout">
        <section className="context-turn-list" aria-label="回答列表">
          <div className="context-pane-heading"><div><h2>最近回答</h2></div>
            <div className="segment-control" role="group" aria-label="回答评价筛选">
              <button type="button" aria-pressed={!negativeOnly} className={!negativeOnly ? 'active' : ''} onClick={() => setNegativeOnly(false)}>全部</button>
              <button type="button" aria-pressed={negativeOnly} className={negativeOnly ? 'active' : ''} onClick={() => setNegativeOnly(true)}>有差评</button>
            </div>
          </div>
          {filterError && <div className="inline-error">{filterError}</div>}
          <div className="context-turn-scroll">
            {items.map((item) => (
              <button
                type="button"
                className={selectedTurn === Number(item.turn_id) ? 'context-turn active' : 'context-turn'}
                key={item.turn_id}
                onClick={() => setSelectedTurn(Number(item.turn_id))}
              >
                <span className="context-turn-top"><strong>{item.turn_handle}</strong><time>{fmtTime(item.created_at)}</time></span>
                <span className="context-turn-topic">{item.current_topic || '未识别话题'}</span>
                <ReactionVotes votes={item.votes} />
                <span className="context-turn-meta"><StatusBadge value={item.feedback?.verdict ?? item.status} label={item.feedback?.verdict === 'correct' ? '答对了' : item.feedback?.verdict === 'off_topic' ? '答非所问' : ROUTE_LABELS[item.route] ?? item.route} /><span>{fmtNumber(item.context_tokens)} Token</span><span>{item.selected_candidates}/{item.candidate_count} 条证据</span></span>
              </button>
            ))}
            {!items.length && <EmptyState>{negativeOnly ? '没有差评记录' : '还没有上下文决策记录'}</EmptyState>}
          </div>
        </section>

        <section className="context-inspector" aria-label="上下文决策详情">
          {loadingDetail && !detail && <EmptyState>正在读取这次回答的上下文...</EmptyState>}
          {detailError && <div className="inline-error">{detailError}</div>}
          {detail && (
            <>
              <div className="context-pane-heading inspector-title">
                <div><span className="eyebrow">{detail.turn_handle} · {detail.scope_key}</span><h2>{detail.current_topic || '未识别话题'}</h2><p>上下文方式：{ROUTE_LABELS[detail.route] ?? detail.route} · 原分类 {ROUTE_LABELS[detail.recall_route?.classifier_mode] ?? detail.recall_route?.classifier_mode ?? '无'} · {detail.profile}/{detail.model}</p></div>
                <StatusBadge value={detail.evidence_guard?.sufficient ? 'succeeded' : 'warning'} label={detail.evidence_guard?.sufficient ? '证据充足' : '证据不足'} />
              </div>

              <div className="context-token-panel">
                <div className="context-token-head"><div><h3>上下文 Token</h3><p>实际放进提示词的聊天与记忆，不包含模型回答</p></div><strong title={`本轮上下文共 ${fmtNumber(tokenTotal)} Token`}>{fmtNumber(tokenTotal)}</strong></div>
                <div className="token-segments" aria-label="上下文 Token 分区">
                  {tokenSegments.filter((segment) => segment.value > 0).map((segment) => <span key={segment.key} style={{ width: `${tokenTotal ? Math.max(segment.value / tokenTotal * 100, 2) : 0}%`, background: segment.color }} title={`${segment.label}：${fmtNumber(segment.value)} Token`} />)}
                </div>
                <div className="token-legend">{tokenSegments.map((segment) => <span key={segment.key}><i style={{ background: segment.color }} />{segment.label} <strong>{fmtNumber(segment.value)}</strong></span>)}</div>
              </div>

              <div className="memory-lanes">
                <div className={detail.memory_usage?.group?.used ? 'memory-lane used' : 'memory-lane'}><UsersRound size={18} /><span><strong>群公共记忆</strong><small>{detail.memory_usage?.group?.used ? `用了 ${rows(detail.memory_usage.group.handles).length} 条` : '本轮没有使用'}</small></span></div>
                <div className={detail.memory_usage?.user?.used ? 'memory-lane used' : 'memory-lane'}><UserRound size={18} /><span><strong>当前用户记忆</strong><small>{detail.memory_usage?.user?.used ? `用了 ${rows(detail.memory_usage.user.handles).length} 条` : '本轮没有使用'}</small></span></div>
              </div>

              <Section title="最终关联的原始消息" description="这些是真正可见且被选入话题链的群消息">
                <div className="evidence-timeline">
                  {rows(detail.evidence_messages).map((message) => <article className="evidence-message" key={message.message_id}><div><span><strong>{message.sender_display || message.sender_user_id}</strong><code>msg#{message.message_id}</code></span><time>{fmtTime(message.occurred_at)}</time></div><p>{message.text || '[非文本消息]'}</p><footer>{rows(message.roles).map((role) => <span key={role}>{role === 'current' ? '当前问题' : role === 'focus' ? '核心焦点' : role}</span>)}{message.reply_to_message_id && <span>回复 msg#{message.reply_to_message_id}</span>}</footer></article>)}
                  {!rows(detail.evidence_messages).length && <EmptyState>原消息已隐藏、清理，或本轮没有关联历史消息</EmptyState>}
                </div>
              </Section>

              <Section title="实际上下文与取舍" description="时间线模式展示真正送入模型的消息；检索模式同时展示评分和淘汰原因">
                <DataTable><thead><tr><th>结果</th><th>候选</th><th>来源</th><th>原始分</th><th>最终分</th><th>为什么</th></tr></thead><tbody>{rows(detail.candidates).map((candidate, index) => <tr key={`${candidate.handle}-${index}`}><td>{candidate.selected ? <span className="decision-selected"><Check size={14} />选中</span> : <span className="decision-dropped"><CircleHelp size={14} />丢弃</span>}</td><td><code>{candidate.handle}</code><small className="cell-sub candidate-preview">{candidate.omitted_count ? `另有 ${fmtNumber(candidate.omitted_count)} 条低分候选未展开` : candidate.content_preview || '内容因隔离策略未展示'}</small></td><td>{SOURCE_LABELS[candidate.source] ?? candidate.source}</td><td>{Number(candidate.raw_score ?? 0).toFixed(3)}</td><td>{Number(candidate.adjusted_score ?? 0).toFixed(3)}</td><td><div className="decision-reasons">{rows(candidate.decision_codes).map((reason) => <span title={reason} key={reason}>{REASON_LABELS[reason] ?? reason}</span>)}</div></td></tr>)}</tbody></DataTable>
                {!rows(detail.candidates).length && <EmptyState>这次回答没有产生召回候选</EmptyState>}
              </Section>

              <Section title="Historian 状态" description="群安静后自动把旧聊天整理成可搜索章节">
                <div className="historian-line"><BrainCircuit size={18} /><span>积压 {fmtNumber((detailHistorian ?? historian).backlog)} · 重试 {fmtNumber((detailHistorian ?? historian).retrying)} · 失败 {fmtNumber((detailHistorian ?? historian).failed)}</span></div>
                {rows((detailHistorian ?? historian).items).slice(0, 5).map((job) => <div className="historian-job" key={job.job_id}><code>{job.handle}</code><StatusBadge value={job.status} /><span>第 {job.attempts}/{job.max_attempts} 次</span><span>{job.last_error || '等待执行'}</span></div>)}
              </Section>

              <Section title="标记回答质量" description="反馈会保存版本号并写入审计记录，可用于后续上下文评测集">
                <ReactionVotes votes={detail.votes} />
                <div className="context-feedback">
                  <label><MessageSquareText size={16} /><input value={note} onChange={(event) => { noteDraft.current.dirty = true; noteDraft.current.revision++; setNote(event.target.value) }} placeholder="可选：写下正确话题或答错原因" /></label>
                  <button className={detail.feedback?.verdict === 'correct' ? 'feedback-button correct active' : 'feedback-button correct'} type="button" onClick={() => void setFeedback('correct')}><ThumbsUp size={16} />答对了</button>
                  <button className={detail.feedback?.verdict === 'off_topic' ? 'feedback-button wrong active' : 'feedback-button wrong'} type="button" onClick={() => void setFeedback('off_topic')}><ThumbsDown size={16} />答非所问</button>
                </div>
              </Section>
            </>
          )}
          {!detail && !loadingDetail && !detailError && <EmptyState>从左侧选择一次回答开始调试</EmptyState>}
        </section>
      </div>
    </>
  )
}
