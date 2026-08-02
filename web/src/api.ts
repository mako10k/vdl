/** vdl API client (proxied to FastAPI in dev). */

const BASE = ''

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = j.detail || JSON.stringify(j)
    } catch {
      /* ignore */
    }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return res.json() as Promise<T>
}

export type Job = {
  id: number
  kind: string
  priority: number
  status: string
  download_id: number | null
  depends_on_job_id: number | null
  progress_pct: number | null
  progress_msg: string | null
  progress_label: string
  error: string | null
  payload: Record<string, unknown>
  result: Record<string, unknown>
  started_at: string | null
  updated_at: string
  finished_at: string | null
}

export type Download = {
  id: number
  url: string
  title: string | null
  status: string
  source_type: string
  file_path: string | null
  file_size: number | null
  error: string | null
  index?: {
    overall_pct: number
    scenes: number
    clip_embeddings: number
    text_embeddings: number
    message: string
  }
}

export type Candidate = {
  index: number
  url: string
  kind: string
  source: string
  title: string | null
  downloaded: boolean
  download_id: number | null
}

export type SearchHit = {
  rank: number
  download_id: number
  scene_index: number | null
  score: number
  title: string | null
  path: string | null
  url: string | null
  document: string
  start_sec: number | null
  end_sec: number | null
  modality: string
}

export const api = {
  health: () => req<{ status: string }>('/api/health'),
  info: () => req<Record<string, unknown>>('/api/info'),
  status: () =>
    req<{
      supervisor: { running: boolean; pid: number | null }
      worker: { running: boolean; pid: number | null; workers?: number }
      queue_counts: Record<string, number>
    }>('/api/status'),
  queue: (params?: { all_done?: boolean; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.all_done) q.set('all_done', 'true')
    if (params?.limit) q.set('limit', String(params.limit))
    const s = q.toString()
    return req<{
      counts: Record<string, number>
      meta: Record<string, unknown>
      jobs: Job[]
    }>(`/api/queue${s ? `?${s}` : ''}`)
  },
  enqueue: (body: {
    url: string
    quality?: string
    force?: boolean
    no_index?: boolean
  }) =>
    req<{ job: Job; runtime: { role: string; pid: number; started: boolean } }>(
      '/api/enqueue',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  enqueueBatch: (urls: string[], opts?: { force?: boolean; no_index?: boolean }) =>
    req<{ jobs: Job[] }>('/api/enqueue/batch', {
      method: 'POST',
      body: JSON.stringify({ urls, ...opts }),
    }),
  candidates: (url: string) =>
    req<{ page_url: string; count: number; candidates: Candidate[] }>(
      '/api/candidates',
      { method: 'POST', body: JSON.stringify({ url }) },
    ),
  registry: () => req<{ downloads: Download[] }>('/api/registry'),
  registryShow: (id: number) => req<Download & { scenes?: unknown[] }>(`/api/registry/${id}`),
  reindex: (id: number) =>
    req<{ jobs: Record<string, number> }>(`/api/registry/${id}/reindex`, {
      method: 'POST',
    }),
  searchText: (query: string, limit = 8) =>
    req<{ hits: SearchHit[] }>('/api/search/text', {
      method: 'POST',
      body: JSON.stringify({ query, limit }),
    }),
  searchSimilar: (download_id: number, modality: 'clip' | 'text' = 'clip', limit = 8) =>
    req<{ hits: SearchHit[] }>('/api/search/similar', {
      method: 'POST',
      body: JSON.stringify({ download_id, modality, limit }),
    }),
  retry: (id: number) =>
    req<Job>(`/api/queue/${id}/retry`, { method: 'POST' }),
  cancel: (id: number) =>
    req<Job>(`/api/queue/${id}/cancel`, { method: 'POST' }),
  fileUrl: (downloadId: number) => `/api/files/${downloadId}`,
}
