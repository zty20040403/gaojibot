import { useId, useMemo, useState } from 'react'
import { fmtNumber } from './components'

interface UsagePoint {
  day: string
  calls: number
  input: number
  output: number
  cached: number
  total: number
}

const WINDOWS = [90, 30, 14] as const
const WIDTH = 1000
const HEIGHT = 300
const PLOT = { left: 58, right: 24, top: 22, bottom: 38 }

function dayKey(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function aggregateUsage(rows: any[], days: number): UsagePoint[] {
  const buckets = new Map<string, UsagePoint>()
  for (const row of rows) {
    const day = String(row.day ?? '')
    if (!day) continue
    const bucket = buckets.get(day) ?? { day, calls: 0, input: 0, output: 0, cached: 0, total: 0 }
    bucket.calls += Number(row.calls ?? 0)
    bucket.input += Number(row.input_tokens ?? 0)
    bucket.output += Number(row.output_tokens ?? 0)
    bucket.cached += Number(row.cached_tokens ?? 0)
    bucket.total = bucket.input + bucket.output
    buckets.set(day, bucket)
  }

  const points: UsagePoint[] = []
  const today = new Date()
  today.setHours(12, 0, 0, 0)
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = new Date(today)
    date.setDate(today.getDate() - offset)
    const day = dayKey(date)
    points.push(buckets.get(day) ?? { day, calls: 0, input: 0, output: 0, cached: 0, total: 0 })
  }
  return points
}

function compact(value: number): string {
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function cacheRate(point: { input: number; cached: number }): number {
  return point.input > 0 ? Math.min(Math.max(point.cached / point.input, 0), 1) * 100 : 0
}

function linePath(points: UsagePoint[], value: (point: UsagePoint) => number, max: number): string {
  const plotWidth = WIDTH - PLOT.left - PLOT.right
  const plotHeight = HEIGHT - PLOT.top - PLOT.bottom
  return points.map((point, index) => {
    const x = PLOT.left + (index / Math.max(points.length - 1, 1)) * plotWidth
    const y = PLOT.top + plotHeight - (value(point) / Math.max(max, 1)) * plotHeight
    return `${index ? 'L' : 'M'} ${x.toFixed(2)} ${y.toFixed(2)}`
  }).join(' ')
}

export function TokenUsageChart({ rows, initialDays = 30 }: { rows: any[]; initialDays?: 14 | 30 | 90 }) {
  const [days, setDays] = useState<14 | 30 | 90>(initialDays)
  const [hovered, setHovered] = useState<number | null>(null)
  const [metric, setMetric] = useState<'tokens' | 'cache'>('tokens')
  const gradientId = useId().replace(/:/g, '')
  const points = useMemo(() => aggregateUsage(rows, days), [rows, days])
  const totals = useMemo(() => points.reduce((sum, point) => ({
    calls: sum.calls + point.calls,
    input: sum.input + point.input,
    output: sum.output + point.output,
    cached: sum.cached + point.cached,
    total: sum.total + point.total,
  }), { calls: 0, input: 0, output: 0, cached: 0, total: 0 }), [points])
  const tokenMax = Math.max(...points.map((point) => point.total), 1)
  const callMax = Math.max(...points.map((point) => point.calls), 1)
  const tokenLine = linePath(points, (point) => point.total, tokenMax)
  const callLine = linePath(points, (point) => point.calls, callMax)
  const cacheMode = metric === 'cache'
  const cacheLine = linePath(points, cacheRate, 100)
  const chartMax = cacheMode ? 100 : tokenMax
  const baseline = HEIGHT - PLOT.bottom
  const areaPath = `${cacheMode ? cacheLine : tokenLine} L ${WIDTH - PLOT.right} ${baseline} L ${PLOT.left} ${baseline} Z`
  const plotWidth = WIDTH - PLOT.left - PLOT.right
  const selected = hovered === null ? null : points[hovered]
  const selectedX = hovered === null ? 0 : PLOT.left + (hovered / Math.max(points.length - 1, 1)) * plotWidth
  const selectedY = selected ? PLOT.top + (HEIGHT - PLOT.top - PLOT.bottom) - ((cacheMode ? cacheRate(selected) : selected.total) / chartMax) * (HEIGHT - PLOT.top - PLOT.bottom) : 0
  const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 4), Math.floor((points.length - 1) / 2), Math.floor((points.length - 1) * 3 / 4), points.length - 1])]

  return (
    <section className="usage-panel">
      <div className="usage-heading">
        <div>
          <h2>{cacheMode ? '缓存命中率' : '模型调用趋势'}</h2>
          <p>{days} 天内 {fmtNumber(totals.calls)} 次调用 · {cacheMode ? `${totals.input ? cacheRate(totals).toFixed(1) + '%' : '—'} 缓存命中` : `${fmtNumber(totals.total)} Token`}</p>
        </div>
        <div className="usage-controls">
        <div className="segment-control" role="group" aria-label="统计指标">
          <button type="button" aria-pressed={!cacheMode} className={!cacheMode ? 'active' : ''} onClick={() => setMetric('tokens')}>用量</button>
          <button type="button" aria-pressed={cacheMode} className={cacheMode ? 'active' : ''} onClick={() => setMetric('cache')}>缓存命中率</button>
        </div>
        <div className="segment-control" role="group" aria-label="用量统计周期">
          {WINDOWS.map((windowDays) => <button key={windowDays} type="button" className={days === windowDays ? 'active' : ''} onClick={() => { setDays(windowDays); setHovered(null) }}>{windowDays} 天</button>)}
        </div>
        </div>
      </div>
      <div className="usage-chart-region" onMouseLeave={() => setHovered(null)}>
        <div className="usage-chart-wrap">
        <svg className="usage-chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${days} 天${cacheMode ? '缓存命中率' : '模型 Token 和调用次数'}趋势`}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#18181b" stopOpacity="0.16" />
              <stop offset="100%" stopColor="#18181b" stopOpacity="0.01" />
            </linearGradient>
          </defs>
          {[0, 1, 2, 3, 4].map((line) => {
            const y = PLOT.top + (line / 4) * (HEIGHT - PLOT.top - PLOT.bottom)
            const value = chartMax * (4 - line) / 4
            return <g key={line}><line className="chart-grid-line" x1={PLOT.left} y1={y} x2={WIDTH - PLOT.right} y2={y} /><text className="chart-axis-label" x={PLOT.left - 10} y={y + 4} textAnchor="end">{cacheMode ? `${value}%` : compact(value)}</text></g>
          })}
          {labelIndexes.map((index) => {
            const x = PLOT.left + (index / Math.max(points.length - 1, 1)) * plotWidth
            return <text className="chart-axis-label" key={index} x={x} y={HEIGHT - 11} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}>{points[index]?.day.slice(5).replace('-', '/')}</text>
          })}
          <path d={areaPath} fill={`url(#${gradientId})`} />
          {cacheMode ? <path className="cache-line" d={cacheLine} /> : <><path className="token-line" d={tokenLine} /><path className="calls-line" d={callLine} /></>}
          {selected && <g className="chart-selection"><line x1={selectedX} y1={PLOT.top} x2={selectedX} y2={baseline} /><circle cx={selectedX} cy={selectedY} r="5" /></g>}
          {points.map((point, index) => {
            const bandWidth = plotWidth / points.length
            const x = PLOT.left + (index / Math.max(points.length - 1, 1)) * plotWidth
            return <rect
              key={point.day}
              className="usage-hit-area"
              x={Math.max(PLOT.left, x - bandWidth / 2)}
              y={PLOT.top}
              width={bandWidth + 1}
              height={baseline - PLOT.top}
              tabIndex={0}
              role="button"
              aria-label={`${point.day}，总 Token ${point.total}，输入 ${point.input}，输出 ${point.output}，缓存 ${point.cached}，命中率 ${point.input ? cacheRate(point).toFixed(1) + '%' : '无输入'}，调用 ${point.calls} 次`}
              onMouseEnter={() => setHovered(index)}
              onFocus={() => setHovered(index)}
              onClick={() => setHovered(index)}
              onBlur={() => setHovered(null)}
            />
          })}
        </svg>
        </div>
        {selected && <div role="tooltip" className={`usage-tooltip ${selectedX > WIDTH * 0.72 ? 'align-right' : ''}`} style={{ left: `${selectedX / WIDTH * 100}%` }}>
          <strong>{selected.day}</strong>
          <span><i className="legend-dot token" />总 Token <b>{fmtNumber(selected.total)}</b></span>
          <span>输入 Token <b>{fmtNumber(selected.input)}</b></span>
          <span>输出 Token <b>{fmtNumber(selected.output)}</b></span>
          <span>缓存命中 <b>{fmtNumber(selected.cached)}</b></span>
          <span>缓存命中率 <b>{selected.input ? cacheRate(selected).toFixed(1) + '%' : '—'}</b></span>
          <span><i className="legend-dot calls" />调用次数 <b>{fmtNumber(selected.calls)}</b></span>
        </div>}
      </div>
      <div className="chart-legend">{cacheMode ? <span><i className="legend-line cache" />缓存命中率</span> : <><span><i className="legend-line token" />Token</span><span><i className="legend-line calls" />调用次数</span></>}</div>
    </section>
  )
}
