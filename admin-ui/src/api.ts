export type ResourceName =
  | 'status'
  | 'qqOfficial'
  | 'accounts'
  | 'approvals'
  | 'securityAudit'
  | 'overview'
  | 'observability'
  | 'alerts'
  | 'deliveries'
  | 'usage'
  | 'tasks'
  | 'subagents'
  | 'jobs'
  | 'sandboxes'
  | 'stickers'
  | 'media'
  | 'sources'
  | 'databases'
  | 'fleet'
  | 'groups'
  | 'localModel'
  | 'tools'
  | 'traces'
  | 'contextPlans'
  | 'contextDebug'
  | 'wiki'
  | 'projects'
  | 'mcp'
  | 'audit'
  | 'versions'

export type JsonObject = Record<string, any>

export interface RealtimeEvent {
  type: 'ready' | 'resources.changed'
  sequence: number
  resources: string[]
  timestamp: number
  versions?: Record<string, number>
}

export const RESOURCE_PATHS: Record<ResourceName, string> = {
  status: '/status',
  qqOfficial: '/qq-official/status',
  accounts: '/accounts',
  approvals: '/approvals',
  securityAudit: '/security-audit',
  overview: '/overview',
  observability: '/observability',
  alerts: '/alerts?days=1&limit=200',
  deliveries: '/deliveries',
  usage: '/usage?days=90',
  tasks: '/tasks',
  subagents: '/subagents',
  jobs: '/jobs',
  sandboxes: '/sandboxes',
  stickers: '/stickers',
  media: '/media',
  sources: '/sources',
  databases: '/databases',
  fleet: '/fleet',
  groups: '/group-models',
  localModel: '/local-model',
  tools: '/tools',
  traces: '/traces',
  contextPlans: '/context-plans',
  contextDebug: '/context-debug',
  wiki: '/wiki',
  projects: '/projects',
  mcp: '/mcp',
  audit: '/audit',
  versions: '/resource-versions',
}

export const EVENT_RESOURCES: Record<string, ResourceName[]> = {
  'qq-official': ['qqOfficial'],
  overview: ['overview'],
  observability: ['observability'],
  alerts: ['alerts', 'observability'],
  deliveries: ['deliveries'],
  usage: ['usage'],
  tasks: ['tasks'],
  subagents: ['subagents', 'tasks'],
  jobs: ['jobs'],
  sandboxes: ['sandboxes'],
  stickers: ['stickers'],
  media: ['media'],
  sources: ['sources'],
  databases: ['databases'],
  fleet: ['fleet'],
  groups: ['groups'],
  'local-model': ['localModel'],
  tools: ['tools'],
  traces: ['traces'],
  'context-debug': ['contextDebug', 'contextPlans'],
  wiki: ['wiki'],
  projects: ['projects'],
  mcp: ['mcp'],
  audit: ['audit'],
}

export class AdminApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `管理 API 返回 ${status}`)
    this.status = status
    this.detail = detail
  }
}

export class AdminClient {
  constructor(
    private readonly runtime: GaojiAdminRuntime,
  ) {}

  async resource(name: ResourceName, signal?: AbortSignal): Promise<JsonObject> {
    return this.request(RESOURCE_PATHS[name], { signal })
  }

  async query(path: string, signal?: AbortSignal): Promise<JsonObject> {
    return this.request(path, { signal })
  }

  async mutate(
    path: string,
    method: 'POST' | 'PUT' | 'DELETE',
    body: unknown,
    expectedVersion: number | undefined,
    onPending?: (pending: JsonObject | null) => void,
  ): Promise<JsonObject> {
    const headers = this.headers()
    headers.set('Content-Type', 'application/json')
    if (expectedVersion !== undefined) {
      headers.set('If-Match', `"${expectedVersion}"`)
    }
    const response = await fetch(`${this.runtime.apiBase}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const payload = await this.decode(response)
    if (!payload.approval_id) return payload
    onPending?.(payload)
    try {
      while (true) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500))
        const pending = await this.query(`/approvals/${encodeURIComponent(payload.approval_id)}`)
        onPending?.(pending)
        if (pending.status === 'succeeded') return pending.kind === 'fleet' ? pending.result.data : pending.result
        const errors: Record<string, string> = {
          failed: '操作执行失败', expired: '口令已过期，请在待确认操作中重发',
          cancelled: '操作已取消或账户权限发生变化', locked: '口令输错 5 次，已作废',
          delivery_failed: 'QQ 私聊发送失败，操作未执行',
          needs_attention: '执行结果需要核对，系统不会自动重放',
        }
        if (errors[pending.status]) {
          throw new AdminApiError(pending.result?.status_code ?? 409,
            pending.result?.detail ?? pending.result?.error ?? errors[pending.status])
        }
      }
    } finally { onPending?.(null) }
  }

  async login(username: string, password: string): Promise<JsonObject> {
    return this.request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }), headers: { 'Content-Type': 'application/json' } })
  }

  async logout(): Promise<void> {
    await this.request('/auth/logout', { method: 'POST' })
  }

  async approvalAction(id: string, action: 'resend' | 'cancel'): Promise<JsonObject> {
    return this.request(`/approvals/${encodeURIComponent(id)}/${action}`, { method: 'POST' })
  }

  async events(
    signal: AbortSignal,
    onEvent: (event: RealtimeEvent) => void,
  ): Promise<void> {
    const response = await fetch(`${this.runtime.apiBase}/events`, {
      headers: this.headers(),
      cache: 'no-store',
      signal,
    })
    if (!response.ok || !response.body) {
      await this.decode(response)
      return
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (!signal.aborted) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = block
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .join('')
        if (data) {
          try {
            onEvent(JSON.parse(data) as RealtimeEvent)
          } catch {
            // Ignore one malformed event; the next resource event is independent.
          }
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  }

  private async request(path: string, init: RequestInit = {}): Promise<JsonObject> {
    const response = await fetch(`${this.runtime.apiBase}${path}`, {
      ...init,
      headers: this.headers(init.headers),
      cache: 'no-store',
    })
    return this.decode(response)
  }

  private headers(existing?: HeadersInit): Headers {
    const headers = new Headers(existing)
    headers.set('Accept', 'application/json')
    const csrf = document.cookie.split('; ').find((part) => part.startsWith('gaoji_csrf='))?.split('=').slice(1).join('=')
    if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf))
    return headers
  }

  private async decode(response: Response): Promise<JsonObject> {
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new AdminApiError(response.status, payload.detail ?? payload)
    }
    return payload as JsonObject
  }
}
