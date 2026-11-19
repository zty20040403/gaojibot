export type ResourceName =
  | 'status'
  | 'qqOfficial'
  | 'accounts'
  | 'securityAudit'
  | 'overview'
  | 'deliveries'
  | 'usage'
  | 'tasks'
  | 'backgroundAgents'
  | 'commands'
  | 'jobs'
  | 'sandboxes'
  | 'stickers'
  | 'media'
  | 'sources'
  | 'groups'
  | 'conversations'
  | 'budgets'
  | 'browser'
  | 'localModel'
  | 'tools'
  | 'traces'
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
  securityAudit: '/security-audit',
  overview: '/overview',
  deliveries: '/deliveries',
  usage: '/usage?days=90',
  tasks: '/tasks',
  backgroundAgents: '/background-agents',
  commands: '/background-commands',
  jobs: '/jobs',
  sandboxes: '/sandboxes',
  stickers: '/stickers',
  media: '/media',
  sources: '/sources',
  groups: '/group-models',
  conversations: '/conversations',
  budgets: '/conversations/budgets',
  browser: '/conversations/browser',
  localModel: '/local-model',
  tools: '/tools',
  traces: '/traces',
  audit: '/audit',
  versions: '/resource-versions',
}

export const EVENT_RESOURCES: Record<string, ResourceName[]> = {
  'qq-official': ['qqOfficial'],
  overview: ['overview'],
  deliveries: ['deliveries'],
  usage: ['usage'],
  tasks: ['tasks'],
  backgroundAgents: ['backgroundAgents', 'tasks'],
  commands: ['commands'],
  jobs: ['jobs'],
  sandboxes: ['sandboxes'],
  stickers: ['stickers'],
  media: ['media'],
  captions: ['media'],
  sources: ['sources'],
  groups: ['groups'],
  conversations: ['conversations'],
  budgets: ['budgets'],
  browser: ['browser'],
  'local-model': ['localModel'],
  tools: ['tools'],
  traces: ['traces'],
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
    return this.decode(response)
  }

  async login(username: string, password: string): Promise<JsonObject> {
    return this.request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }), headers: { 'Content-Type': 'application/json' } })
  }

  async logout(): Promise<void> {
    await this.request('/auth/logout', { method: 'POST' })
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
