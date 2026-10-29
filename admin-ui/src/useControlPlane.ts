import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AdminApiError,
  AdminClient,
  EVENT_RESOURCES,
  type JsonObject,
  type ResourceName,
  type RealtimeEvent,
} from './api'

const INITIAL_RESOURCES: ResourceName[] = [
  'qqOfficial',
  'accounts',
  'approvals',
  'securityAudit',
  'overview',
  'observability',
  'alerts',
  'deliveries',
  'usage',
  'tasks',
  'subagents',
  'jobs',
  'sandboxes',
  'stickers',
  'media',
  'sources',
  'databases',
  'fleet',
  'groups',
  'localModel',
  'tools',
  'traces',
  'contextPlans',
  'contextDebug',
  'wiki',
  'projects',
  'mcp',
  'audit',
  'versions',
]

export function useControlPlane(runtime: GaojiAdminRuntime) {
  const [user, setUser] = useState<JsonObject | null>(null)
  const [checkingSession, setCheckingSession] = useState(true)
  const [pendingApproval, setPendingApproval] = useState<JsonObject | null>(null)
  const [data, setData] = useState<Partial<Record<ResourceName, JsonObject>>>({})
  const [versions, setVersions] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState<Set<ResourceName>>(new Set())
  const [online, setOnline] = useState(false)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState(0)
  const eventSequence = useRef(0)
  const client = useMemo(() => new AdminClient(runtime), [runtime])
  const authenticated = user !== null
  const isAdmin = user?.role === 'admin'
  const initialResources = useMemo<ResourceName[]>(() => isAdmin ? INITIAL_RESOURCES : ['status'], [isAdmin])

  const login = useCallback(async (username: string, password: string) => {
    setError('')
    try {
      const result = await client.login(username, password)
      setUser(result.account)
      setData({})
      setVersions({})
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败')
      throw reason
    }
  }, [client])

  const logout = useCallback(async () => {
    try { await client.logout() } finally {
      setUser(null); setData({}); setVersions({}); setOnline(false); setPendingApproval(null)
    }
  }, [client])

  useEffect(() => {
    localStorage.removeItem('gaoji.admin.token')
    const controller = new AbortController()
    void client.query('/me', controller.signal).then((result) => setUser(result.account)).catch((reason) => {
      if (!controller.signal.aborted && (!(reason instanceof AdminApiError) || reason.status !== 401)) setError(reason.message)
    }).finally(() => { if (!controller.signal.aborted) setCheckingSession(false) })
    return () => controller.abort()
  }, [client])

  const refresh = useCallback(
    async (resource: ResourceName, signal?: AbortSignal) => {
      setLoading((current) => new Set(current).add(resource))
      try {
        const payload = await client.resource(resource, signal)
        setData((current) => ({ ...current, [resource]: payload }))
        if (resource === 'versions' && payload.versions) {
          setVersions(payload.versions as Record<string, number>)
        } else if (typeof payload.resource === 'string' && typeof payload.resource_version === 'number') {
          setVersions((current) => ({
            ...current,
            [payload.resource]: payload.resource_version,
          }))
        }
        setOnline(true)
        setUpdatedAt(Date.now())
        setError('')
      } catch (reason) {
        if (signal?.aborted) return
        setOnline(false)
        if (reason instanceof AdminApiError && reason.status === 401) {
          setUser(null)
          setData({})
          setError('登录已过期，请重新登录')
        } else {
          setError(reason instanceof Error ? reason.message : '管理 API 暂时不可用')
        }
      } finally {
        setLoading((current) => {
          const next = new Set(current)
          next.delete(resource)
          return next
        })
      }
    },
    [client],
  )

  const refreshMany = useCallback(
    async (resources: ResourceName[], signal?: AbortSignal) => {
      await Promise.allSettled([...new Set(resources)].map((resource) => refresh(resource, signal)))
    },
    [refresh],
  )
  const refreshAll = useCallback(() => refreshMany(initialResources), [refreshMany, initialResources])
  const query = useCallback(
    (path: string, signal?: AbortSignal) => client.query(path, signal),
    [client],
  )

  useEffect(() => {
    if (!authenticated) return
    const controller = new AbortController()
    void refreshMany(initialResources, controller.signal)
    return () => controller.abort()
  }, [authenticated, refreshMany, initialResources])

  useEffect(() => {
    if (!authenticated) return
    const controller = new AbortController()
    if (!isAdmin) return
    const pendingResources = new Set<ResourceName>()
    let refreshTimer: number | null = null
    let retry = 1000
    const flushResources = () => {
      refreshTimer = null
      const resources = [...pendingResources]
      pendingResources.clear()
      if (resources.length) void refreshMany(resources, controller.signal)
    }
    const run = async () => {
      while (!controller.signal.aborted) {
        try {
          await client.events(controller.signal, (event: RealtimeEvent) => {
            if (event.type === 'ready') {
              eventSequence.current = event.sequence || 0
              if (event.versions) setVersions((current) => ({ ...current, ...event.versions }))
              return
            }
            if (event.sequence && event.sequence <= eventSequence.current) return
            eventSequence.current = Math.max(eventSequence.current, event.sequence || 0)
            if (event.versions) setVersions((current) => ({ ...current, ...event.versions }))
            if (event.type === 'resources.changed') {
              const resources = event.resources.flatMap((resource) => EVENT_RESOURCES[resource] ?? [])
              if (event.versions) resources.push('audit', 'versions')
              resources.forEach((resource) => pendingResources.add(resource))
              if (refreshTimer === null) {
                refreshTimer = window.setTimeout(flushResources, 80)
              }
            }
          })
          retry = 1000
        } catch (reason) {
          if (controller.signal.aborted) break
          setOnline(false)
          setError(reason instanceof Error ? reason.message : '实时连接已断开')
        }
        await new Promise((resolve) => window.setTimeout(resolve, retry))
        retry = Math.min(retry * 2, 15000)
      }
    }
    void run()
    return () => {
      controller.abort()
      if (refreshTimer !== null) window.clearTimeout(refreshTimer)
    }
  }, [authenticated, isAdmin, client, refreshMany])

  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(() => {
      void refreshMany(isAdmin ? ['qqOfficial', 'overview', 'observability', 'alerts', 'databases', 'fleet', 'usage', 'subagents', 'contextDebug', 'wiki', 'mcp', 'localModel', 'approvals', 'accounts', 'securityAudit'] : ['status'])
    }, 30000)
    return () => window.clearInterval(timer)
  }, [authenticated, refreshMany, initialResources])

  const mutate = useCallback(
    async (
      resource: string,
      path: string,
      method: 'POST' | 'PUT' | 'DELETE',
      body: unknown,
      refreshResources: ResourceName[],
    ) => {
      try {
        const payload = await client.mutate(path, method, body, versions[resource], setPendingApproval)
        if (typeof payload.resource_version === 'number') {
          setVersions((current) => ({ ...current, [resource]: payload.resource_version }))
        }
        await refreshMany([...refreshResources, 'audit', 'versions'])
        setError('')
        return payload
      } catch (reason) {
        if (reason instanceof AdminApiError && reason.status === 401) {
          setUser(null); setData({}); setError('账户已更新或登录已失效，请重新登录')
        } else if (reason instanceof AdminApiError && reason.status === 409 && typeof reason.detail === 'object' && reason.detail !== null && 'code' in reason.detail && reason.detail.code === 'resource_version_conflict') {
          await refreshMany([...refreshResources, 'versions'])
          setError('数据已被其他管理员修改，已加载最新版本，请重试')
        } else {
          setError(reason instanceof Error ? reason.message : '修改失败')
        }
        throw reason
      }
    },
    [client, refreshMany, versions],
  )

  return {
    user,
    isAdmin,
    checkingSession,
    pendingApproval,
    login,
    logout,
    approvalAction: async (id: string, action: 'resend' | 'cancel') => {
      await client.approvalAction(id, action)
      await refreshMany(['approvals'])
    },
    authenticated,
    data,
    versions,
    loading,
    online,
    updatedAt,
    error,
    clearError: () => setError(''),
    refresh,
    refreshMany,
    refreshAll,
    query,
    mutate,
  }
}
