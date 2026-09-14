import type { Facets, Health, Job, JobPage, JobQuery, Preferences, Profile, Settings, Source, Status } from './types'

const PAGE_SIZE = 30

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export function listJobs(query: JobQuery, offset = 0): Promise<JobPage> {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
    sort: query.sort,
    min_score: String(query.minScore),
  })
  if (query.q.trim()) params.set('q', query.q.trim())
  if (query.status) params.set('status', query.status)
  if (query.remote) params.set('remote', query.remote)
  if (query.source) params.set('source', query.source)
  if (query.includeArchived) params.set('include_archived', 'true')
  return request<JobPage>(`/jobs?${params}`)
}

export const getFacets = () => request<Facets>('/jobs/facets')
export const getHealth = () => request<Health>('/health')
export const getJob = (id: number) => request<Job>(`/jobs/${id}`)

export const setStatus = (id: number, status: Status, extra: { notes?: string; reason?: string } = {}) =>
  request<{ ok: true }>(`/jobs/${id}/state`, {
    method: 'PATCH',
    body: JSON.stringify({ status, ...extra }),
  })

export const deleteJob = (id: number) =>
  request<{ ok: true }>(`/jobs/${id}`, { method: 'DELETE' })

export const archiveOlderThan = (days: number) =>
  request<{ archived: number }>('/jobs/archive', {
    method: 'POST',
    body: JSON.stringify({ older_than_days: days }),
  })

export const archiveIds = (ids: number[]) =>
  request<{ archived: number }>('/jobs/archive', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })

export const getSettings = () => request<Settings>('/settings')
export const updateSettings = (patch: Partial<Pick<Settings, 'interval_minutes' | 'run_on_start'>>) =>
  request<Settings>('/settings', { method: 'PATCH', body: JSON.stringify(patch) })

export const getProfile = () => request<Profile>('/profile')
export const saveNotes = (text: string) =>
  request<Profile>('/profile/notes', { method: 'PUT', body: JSON.stringify({ text }) })

export async function uploadCv(file: File): Promise<Profile & { saved: string }> {
  const body = new FormData()
  body.append('file', file, file.name)
  // no content-type header: the browser sets the multipart boundary itself
  const res = await fetch('/profile/cv', { method: 'POST', body })
  if (!res.ok) {
    let detail = res.statusText
    try { const j = (await res.json()) as { detail?: unknown }; if (typeof j.detail === 'string') detail = j.detail } catch { /* not JSON */ }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as Profile & { saved: string }
}

export const getSources = () => request<{ sources: Source[] }>('/sources').then((r) => r.sources)
export const addSource = (url: string) =>
  request<{ source_id: string; type: string; open_positions: number }>('/sources', { method: 'POST', body: JSON.stringify({ url }) })
export const setSourceEnabled = (id: string, enabled: boolean) =>
  request<{ ok: true }>(`/sources/${encodeURIComponent(id)}/enabled?enabled=${enabled}`, { method: 'POST' })
export const deleteSource = (id: string) =>
  request<{ ok: true }>(`/sources/${encodeURIComponent(id)}`, { method: 'DELETE' })

export interface PreferencesResponse { preferences: Preferences; config_path: string }
export const getPreferences = () => request<PreferencesResponse>('/preferences')
export const savePreferences = (p: Preferences) =>
  request<PreferencesResponse>('/preferences', { method: 'PUT', body: JSON.stringify(p) })

export const resetJobs = () =>
  request<{ deleted: Record<string, number> }>('/reset/jobs', { method: 'POST', body: JSON.stringify({ confirm: 'DELETE' }) })
export const resetAll = () =>
  request<{ deleted: Record<string, number>; sources_reseeded: number }>('/reset/all', { method: 'POST', body: JSON.stringify({ confirm: 'DELETE' }) })

export const triggerRun = () => request<{ ok: true }>('/runs', { method: 'POST' })

export { ApiError, PAGE_SIZE }
