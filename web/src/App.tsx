import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import './App.css'
import {
  api,
  type Candidate,
  type Download,
  type Job,
  type Scene,
  type SearchHit,
} from './api'

type Tab = 'get' | 'queue' | 'registry' | 'search' | 'candidates'

/** Floating video player target (preview click → play at scene time). */
export type PlayRequest = {
  downloadId: number
  title?: string
  startSec?: number | null
  endSec?: number | null
  sceneIndex?: number | null
  poster?: string | null
}

function StatusBadge({
  label,
  on,
}: {
  label: string
  on: boolean
}) {
  return <span className={`badge ${on ? 'on' : 'off'}`}>{label}</span>
}

function JobStatus({ s }: { s: string }) {
  return <span className={`status ${s}`}>{s}</span>
}

function formatTime(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return '—'
  const s = Math.max(0, sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = Math.floor(s % 60)
  const frac = Math.floor((s % 1) * 10)
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`
  return `${m}:${String(r).padStart(2, '0')}.${frac}`
}

function FloatingPlayer({
  state,
  onClose,
}: {
  state: PlayRequest | null
  onClose: () => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{
    ox: number
    oy: number
    px: number
    py: number
  } | null>(null)
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null)
  const [minimized, setMinimized] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!state) return
    setErr(null)
    setMinimized(false)
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [state?.downloadId, state?.startSec, state?.sceneIndex, onClose])

  // Seek + autoplay when source / start time changes
  useEffect(() => {
    if (!state) return
    const v = videoRef.current
    if (!v) return

    const seekAndPlay = () => {
      const t = state.startSec
      if (t != null && t > 0 && Number.isFinite(t)) {
        try {
          v.currentTime = t
        } catch {
          /* ignore */
        }
      }
      void v.play().catch(() => {
        /* autoplay may be blocked until user interacts — controls still work */
      })
    }

    const onMeta = () => seekAndPlay()
    const onErr = () => setErr('再生できません（ファイル未完了 or 非対応形式）')

    v.addEventListener('loadedmetadata', onMeta)
    v.addEventListener('error', onErr)
    if (v.readyState >= 1) seekAndPlay()

    return () => {
      v.removeEventListener('loadedmetadata', onMeta)
      v.removeEventListener('error', onErr)
    }
  }, [state?.downloadId, state?.startSec, state?.sceneIndex])

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const d = dragRef.current
      if (!d) return
      setPos({ x: d.px + (e.clientX - d.ox), y: d.py + (e.clientY - d.oy) })
    }
    const onUp = () => {
      dragRef.current = null
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [])

  if (!state) return null

  const title =
    state.title ||
    `#${state.downloadId}` +
      (state.sceneIndex != null ? ` · sc ${state.sceneIndex}` : '')

  const style: CSSProperties = pos
    ? { left: pos.x, top: pos.y, right: 'auto', bottom: 'auto' }
    : {}

  const src = api.fileUrl(state.downloadId)
  const videoKey = `${state.downloadId}:${state.startSec ?? 0}:${state.sceneIndex ?? ''}`

  return (
    <div
      ref={panelRef}
      className={`float-player ${minimized ? 'minimized' : ''}`}
      style={style}
      role="dialog"
      aria-label="video player"
    >
      <div
        className="float-player-bar"
        onPointerDown={(e) => {
          if ((e.target as HTMLElement).closest('button')) return
          const rect = panelRef.current?.getBoundingClientRect()
          if (!rect) return
          dragRef.current = {
            ox: e.clientX,
            oy: e.clientY,
            px: rect.left,
            py: rect.top,
          }
          // lock current position so first drag doesn’t jump
          if (!pos) setPos({ x: rect.left, y: rect.top })
        }}
      >
        <span className="float-player-title" title={title}>
          ▶ {title}
        </span>
        <span className="float-player-meta">
          {state.startSec != null || state.endSec != null
            ? `${formatTime(state.startSec)}–${formatTime(state.endSec)}`
            : ''}
        </span>
        <div className="float-player-actions">
          <button
            type="button"
            className="float-btn"
            title={minimized ? 'expand' : 'minimize'}
            onClick={() => setMinimized((m) => !m)}
          >
            {minimized ? '□' : '—'}
          </button>
          <button
            type="button"
            className="float-btn"
            title="close"
            onClick={onClose}
          >
            ×
          </button>
        </div>
      </div>
      {!minimized && (
        <div className="float-player-body">
          {err && <div className="float-player-err">{err}</div>}
          <video
            key={videoKey}
            ref={videoRef}
            className="float-player-video"
            src={src}
            poster={state.poster || undefined}
            controls
            playsInline
            preload="metadata"
          />
          <div className="float-player-footer">
            <a className="link" href={src} target="_blank" rel="noreferrer">
              open file
            </a>
            {state.startSec != null && (
              <button
                type="button"
                className="btn secondary"
                style={{ padding: '0.15rem 0.5rem', fontSize: '0.75rem' }}
                onClick={() => {
                  const v = videoRef.current
                  if (!v || state.startSec == null) return
                  v.currentTime = state.startSec
                  void v.play().catch(() => {})
                }}
              >
                jump to scene
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Thumb({
  src,
  alt,
  size,
  caption,
  onPlay,
}: {
  src?: string | null
  alt: string
  size?: 'sm' | 'lg'
  caption?: string
  onPlay?: () => void
}) {
  const [failed, setFailed] = useState(false)
  if (!src || failed) {
    return (
      <div
        className={`thumb-ph ${size || ''}${onPlay ? ' clickable' : ''}`}
        title={onPlay ? 'play video' : undefined}
        onClick={(e) => {
          e.stopPropagation()
          onPlay?.()
        }}
      >
        {onPlay ? '▶' : 'no img'}
      </div>
    )
  }
  return (
    <img
      className={`thumb ${size || ''}`}
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      onClick={(e) => {
        e.stopPropagation()
        onPlay?.()
      }}
      title={caption || alt}
    />
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>('get')
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const [supOn, setSupOn] = useState(false)
  const [workerOn, setWorkerOn] = useState(false)
  const [counts, setCounts] = useState<Record<string, number>>({})

  const [url, setUrl] = useState('')
  const [quality, setQuality] = useState('best')
  const [busy, setBusy] = useState(false)

  const [jobs, setJobs] = useState<Job[]>([])
  const [allDone, setAllDone] = useState(false)

  const [downloads, setDownloads] = useState<Download[]>([])
  const [detail, setDetail] = useState<Download | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const [candUrl, setCandUrl] = useState('')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [candPage, setCandPage] = useState('')
  const [candNote, setCandNote] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())

  const [searchQ, setSearchQ] = useState('')
  const [similarId, setSimilarId] = useState('')
  const [hits, setHits] = useState<SearchHit[]>([])
  const [searchTitle, setSearchTitle] = useState('')

  const [player, setPlayer] = useState<PlayRequest | null>(null)
  const openPlayer = useCallback((req: PlayRequest) => {
    setPlayer(req)
  }, [])
  const closePlayer = useCallback(() => setPlayer(null), [])

  const refreshStatus = useCallback(async () => {
    try {
      const st = await api.status()
      setSupOn(!!st.supervisor?.running)
      setWorkerOn(!!st.worker?.running)
      setCounts(st.queue_counts || {})
    } catch {
      /* ignore poll errors */
    }
  }, [])

  const refreshQueue = useCallback(async () => {
    try {
      const q = await api.queue({ all_done: allDone, limit: 40 })
      setJobs(q.jobs)
      setCounts(q.counts)
    } catch (e) {
      setErr(String(e))
    }
  }, [allDone])

  const refreshRegistry = useCallback(async () => {
    try {
      const r = await api.registry()
      setDownloads(r.downloads)
    } catch (e) {
      setErr(String(e))
    }
  }, [])

  const openDetail = useCallback(async (id: number) => {
    setDetailLoading(true)
    setErr(null)
    try {
      const d = await api.registryShow(id)
      setDetail(d)
    } catch (e) {
      setErr(String(e))
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshStatus()
    const t = setInterval(refreshStatus, 3000)
    return () => clearInterval(t)
  }, [refreshStatus])

  // Deep-link / extension bridge: ?tab=candidates&vdl_bridge=TOKEN
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const tabParam = params.get('tab') as Tab | null
    const bridge = params.get('vdl_bridge') || params.get('bridge')
    const validTabs: Tab[] = ['get', 'queue', 'registry', 'search', 'candidates']

    let cancelled = false

    async function applyBridge() {
      if (tabParam && validTabs.includes(tabParam)) {
        setTab(tabParam)
      }
      if (!bridge) {
        // still clean empty noise? keep URL if only tab=
        if (tabParam && validTabs.includes(tabParam) && [...params.keys()].length === 1) {
          /* keep */
        }
        return
      }
      try {
        const data = await api.bridgePull(bridge, false)
        if (cancelled) return
        const list = data.candidates || []
        setCandidates(list)
        setCandPage(data.page_url || '')
        setCandUrl(data.page_url || '')
        setCandNote(
          data.note ||
            (data.from === 'extension' ? 'Chrome 拡張から取り込み' : null),
        )
        setSelected(
          new Set(
            list
              .filter((c) =>
                c.selected_by_default != null
                  ? c.selected_by_default
                  : !c.downloaded && !c.known && !c.queued,
              )
              .map((c) => c.index),
          ),
        )
        const nextTab = (data.tab as Tab) || tabParam || 'candidates'
        if (validTabs.includes(nextTab)) setTab(nextTab)
        setMsg(
          `拡張から ${data.count ?? list.length} 件の候補を取り込みました` +
            (data.page_url ? ` · ${data.page_url}` : ''),
        )
        // strip token from address bar (keep tab)
        const clean = new URL(window.location.href)
        clean.searchParams.delete('vdl_bridge')
        clean.searchParams.delete('bridge')
        if (nextTab) clean.searchParams.set('tab', nextTab)
        window.history.replaceState({}, '', clean.pathname + clean.search)
      } catch (e) {
        if (!cancelled) setErr(`bridge: ${String(e)}`)
      }
    }

    void applyBridge()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (tab === 'queue') {
      refreshQueue()
      const t = setInterval(refreshQueue, 2000)
      return () => clearInterval(t)
    }
    if (tab === 'registry') {
      refreshRegistry()
    }
  }, [tab, refreshQueue, refreshRegistry])

  async function onEnqueue() {
    setErr(null)
    setMsg(null)
    if (!url.trim()) return
    setBusy(true)
    try {
      const r = await api.enqueue({ url: url.trim(), quality })
      setMsg(
        `queued job #${r.job.id} · ${r.runtime.role} pid=${r.runtime.pid}${
          r.runtime.started ? ' (started)' : ''
        }`,
      )
      setTab('queue')
      await refreshQueue()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function onCandidates() {
    setErr(null)
    setMsg(null)
    const u = candUrl.trim() || url.trim()
    if (!u) return
    setBusy(true)
    try {
      const r = await api.candidates(u)
      setCandidates(r.candidates)
      setCandPage(r.page_url)
      setCandNote(null)
      setSelected(
        new Set(
          r.candidates
            .filter((c) =>
              c.selected_by_default != null
                ? c.selected_by_default
                : !c.downloaded && !c.known && !c.queued,
            )
            .map((c) => c.index),
        ),
      )
      setMsg(`${r.count} candidate(s) from ${r.page_url}`)
      setTab('candidates')
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function onEnqueueSelected() {
    setErr(null)
    setMsg(null)
    const urls = candidates
      .filter((c) => selected.has(c.index))
      .map((c) => c.url)
    if (!urls.length) {
      setErr('候補が選択されていません')
      return
    }
    setBusy(true)
    try {
      const r = await api.enqueueBatch(urls)
      setMsg(`enqueued ${r.jobs.length} job(s)`)
      setTab('queue')
      await refreshQueue()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function onSearchText() {
    setErr(null)
    setHits([])
    if (!searchQ.trim()) return
    setBusy(true)
    try {
      const r = await api.searchText(searchQ.trim())
      setHits(r.hits)
      setSearchTitle(`text: ${searchQ.trim()}`)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function onSearchSimilar() {
    setErr(null)
    setHits([])
    const id = parseInt(similarId, 10)
    if (!id) {
      setErr('download id を指定してください')
      return
    }
    setBusy(true)
    try {
      const r = await api.searchSimilar(id, 'clip')
      setHits(r.hits)
      setSearchTitle(`similar to dl=${id} [clip]`)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const scenes: Scene[] = detail?.scenes || []
  const scenesWithKf = scenes.filter((s) => s.has_keyframe !== false)

  return (
    <div className="app">
      <header className="app-header">
        <h1>
          <span>vdl</span> console
        </h1>
        <div className="badge-row">
          <StatusBadge
            label={supOn ? `supervisor · on` : 'supervisor · off'}
            on={supOn}
          />
          <StatusBadge
            label={workerOn ? `worker · on` : 'worker · off'}
            on={workerOn}
          />
          {Object.entries(counts).map(([k, v]) => (
            <span key={k} className="badge">
              {k}={v}
            </span>
          ))}
        </div>
      </header>

      <nav className="tabs">
        {(
          [
            ['get', 'Get / Enqueue'],
            ['candidates', 'Candidates'],
            ['queue', 'Queue'],
            ['registry', 'Registry'],
            ['search', 'Search'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={tab === id ? 'active' : ''}
            onClick={() => {
              setTab(id)
              if (id !== 'registry') setDetail(null)
            }}
          >
            {label}
          </button>
        ))}
      </nav>

      {err && <div className="error">{err}</div>}
      {msg && <p className="ok-msg">{msg}</p>}

      {tab === 'get' && (
        <section className="panel">
          <h2>Download URL</h2>
          <p className="muted">
            動画 / m3u8 URL はキュー投入。HTML は Candidates タブで候補抽出 →
            オプトインで投入。
          </p>
          <div className="row">
            <input
              className="grow"
              type="text"
              placeholder="https://…/video.mp4 or …/master.m3u8"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onEnqueue()}
            />
            <select value={quality} onChange={(e) => setQuality(e.target.value)}>
              <option value="best">best</option>
              <option value="worst">worst</option>
              <option value="720">720</option>
              <option value="1080">1080</option>
            </select>
            <button className="btn" type="button" disabled={busy} onClick={onEnqueue}>
              Enqueue
            </button>
            <button
              className="btn secondary"
              type="button"
              disabled={busy}
              onClick={() => {
                setCandUrl(url)
                void onCandidates()
              }}
            >
              Scrape as page
            </button>
          </div>
        </section>
      )}

      {tab === 'candidates' && (
        <section className="panel">
          <h2>Candidates</h2>
          <p className="muted">
            サーバー HTML 抽出、または Chrome 拡張のページ内検出結果（
            <code>?vdl_bridge=…</code>）を表示します。
          </p>
          {candNote && (
            <p className="ok-msg" style={{ marginTop: 0 }}>
              {candNote}
            </p>
          )}
          <div className="row">
            <input
              className="grow"
              type="text"
              placeholder="https://…/watch/123"
              value={candUrl}
              onChange={(e) => setCandUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onCandidates()}
            />
            <button className="btn" type="button" disabled={busy} onClick={onCandidates}>
              Extract
            </button>
            <button
              className="btn ok"
              type="button"
              disabled={busy || !selected.size}
              onClick={onEnqueueSelected}
            >
              Enqueue selected ({selected.size})
            </button>
          </div>
          {candPage && <p className="muted">page: {candPage}</p>}
          {!candidates.length ? (
            <div className="empty">候補がありません。ページ URL を抽出してください。</div>
          ) : (
            <div className="cand-list">
              {candidates.map((c) => {
                const state =
                  c.state ||
                  (c.downloaded
                    ? `済 id=${c.download_id}`
                    : c.queued
                      ? 'キュー中'
                      : c.known
                        ? `登録 id=${c.download_id ?? '?'}`
                        : '新規')
                return (
                  <div className="cand" key={c.index}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selected.has(c.index)}
                        onChange={(e) => {
                          setSelected((prev) => {
                            const n = new Set(prev)
                            if (e.target.checked) n.add(c.index)
                            else n.delete(c.index)
                            return n
                          })
                        }}
                      />
                      <div>
                        <div className="url">
                          <strong>#{c.index}</strong> [{c.kind}] {c.url}
                        </div>
                        <div className="meta">
                          {state} · {c.source}
                          {c.title ? ` · ${c.title}` : ''}
                        </div>
                      </div>
                    </label>
                  </div>
                )
              })}
            </div>
          )}
        </section>
      )}

      {tab === 'queue' && (
        <section className="panel">
          <div className="toolbar">
            <h2>Queue</h2>
            <div className="row" style={{ margin: 0 }}>
              <label className="muted">
                <input
                  type="checkbox"
                  checked={allDone}
                  onChange={(e) => setAllDone(e.target.checked)}
                />{' '}
                show all done
              </label>
              <button className="btn secondary" type="button" onClick={() => refreshQueue()}>
                Refresh
              </button>
            </div>
          </div>
          {!jobs.length ? (
            <div className="empty">ジョブなし</div>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>kind</th>
                  <th>status</th>
                  <th>progress</th>
                  <th>dl</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td>{j.id}</td>
                    <td>{j.kind}</td>
                    <td>
                      <JobStatus s={j.status} />
                    </td>
                    <td className="progress">{j.progress_label}</td>
                    <td>{j.download_id ?? '—'}</td>
                    <td>
                      {j.status === 'failed' && (
                        <button
                          className="btn secondary"
                          type="button"
                          onClick={() => api.retry(j.id).then(refreshQueue)}
                        >
                          retry
                        </button>
                      )}
                      {(j.status === 'pending' || j.status === 'running') && (
                        <button
                          className="btn danger"
                          type="button"
                          onClick={() => api.cancel(j.id).then(refreshQueue)}
                        >
                          cancel
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {tab === 'registry' && (
        <section className="panel">
          <div className="toolbar">
            <h2>Registry</h2>
            <button className="btn secondary" type="button" onClick={() => refreshRegistry()}>
              Refresh
            </button>
          </div>

          {detailLoading && <p className="muted">loading detail…</p>}

          {detail && !detailLoading && (
            <div className="detail-panel">
              <div className="toolbar">
                <h3>
                  #{detail.id} · {detail.title || detail.url}
                </h3>
                <button
                  className="btn secondary"
                  type="button"
                  onClick={() => setDetail(null)}
                >
                  Close detail
                </button>
              </div>
              <div className="detail-head">
                {detail.preview_url ? (
                  <img
                    className="hero"
                    src={detail.preview_url}
                    alt={`preview #${detail.id}`}
                    title="再生"
                    onClick={() =>
                      openPlayer({
                        downloadId: detail.id,
                        title: `#${detail.id} ${detail.title || ''}`.trim(),
                        startSec: 0,
                        poster: detail.preview_url,
                      })
                    }
                  />
                ) : (
                  <div
                    className="thumb-ph clickable"
                    style={{ width: 200, height: 120 }}
                    onClick={() =>
                      openPlayer({
                        downloadId: detail.id,
                        title: `#${detail.id} ${detail.title || ''}`.trim(),
                        startSec: 0,
                      })
                    }
                  >
                    ▶ play
                  </div>
                )}
                <div>
                  <p className="muted" style={{ margin: '0 0 0.4rem' }}>
                    <JobStatus s={detail.status} /> · {detail.source_type}
                    {detail.index
                      ? ` · index ${detail.index.overall_pct.toFixed(0)}% · scenes=${detail.index.scenes}`
                      : ''}
                  </p>
                  <p className="muted" style={{ wordBreak: 'break-all', margin: 0 }}>
                    {detail.file_path || detail.url}
                  </p>
                  <div className="row" style={{ marginTop: '0.6rem' }}>
                    {detail.status === 'completed' && (
                      <>
                        <button
                          className="btn"
                          type="button"
                          onClick={() =>
                            openPlayer({
                              downloadId: detail.id,
                              title: `#${detail.id} ${detail.title || ''}`.trim(),
                              startSec: 0,
                              poster: detail.preview_url,
                            })
                          }
                        >
                          ▶ Play
                        </button>
                        <a
                          className="link"
                          href={api.fileUrl(detail.id)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          open file
                        </a>
                        <button
                          className="btn secondary"
                          type="button"
                          onClick={() =>
                            api.reindex(detail.id).then(() => {
                              setMsg(`reindex queued for #${detail.id}`)
                              setTab('queue')
                            })
                          }
                        >
                          reindex
                        </button>
                        <button
                          className="btn secondary"
                          type="button"
                          onClick={() => {
                            setSimilarId(String(detail.id))
                            setTab('search')
                          }}
                        >
                          similar
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>

              <h4 style={{ margin: '0.5rem 0 0' }}>
                Scenes ({scenes.length}
                {scenesWithKf.length !== scenes.length
                  ? `, ${scenesWithKf.length} with keyframe`
                  : ''}
                )
              </h4>
              {!scenes.length ? (
                <div className="empty">シーンなし（reindex で生成）</div>
              ) : (
                <div className="scene-grid">
                  {scenes.map((s) => {
                    const src =
                      s.has_keyframe === false
                        ? null
                        : s.keyframe_url ||
                          api.keyframeUrl(detail.id, s.scene_index)
                    const cap = `sc#${s.scene_index} ${s.start_sec.toFixed(1)}–${s.end_sec.toFixed(1)}s`
                    const play = () =>
                      openPlayer({
                        downloadId: detail.id,
                        title: `#${detail.id} sc${s.scene_index}`,
                        startSec: s.start_sec,
                        endSec: s.end_sec,
                        sceneIndex: s.scene_index,
                        poster: src,
                      })
                    return (
                      <div
                        className="scene-card"
                        key={s.id ?? s.scene_index}
                        onClick={play}
                        title="このシーンから再生"
                      >
                        {src ? (
                          <img
                            src={src}
                            alt={cap}
                            loading="lazy"
                            onError={(e) => {
                              ;(e.target as HTMLImageElement).style.display = 'none'
                            }}
                          />
                        ) : (
                          <div
                            className="thumb-ph"
                            style={{ width: '100%', height: 90, border: 'none' }}
                          >
                            ▶
                          </div>
                        )}
                        <div className="cap">{cap}</div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {!downloads.length ? (
            <div className="empty">ダウンロードなし</div>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>prev</th>
                  <th>ID</th>
                  <th>status</th>
                  <th>type</th>
                  <th>index</th>
                  <th>title / path</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {downloads.map((d) => (
                  <tr
                    key={d.id}
                    className={detail?.id === d.id ? 'selected-row' : ''}
                    style={{ cursor: d.status === 'completed' ? 'pointer' : undefined }}
                    onClick={() => {
                      if (d.status === 'completed') void openDetail(d.id)
                    }}
                  >
                    <td onClick={(e) => e.stopPropagation()}>
                      <Thumb
                        src={d.preview_url}
                        alt={`#${d.id}`}
                        size="sm"
                        caption={`▶ #${d.id} ${d.title || ''}`.trim()}
                        onPlay={
                          d.status === 'completed'
                            ? () =>
                                openPlayer({
                                  downloadId: d.id,
                                  title: `#${d.id} ${d.title || ''}`.trim(),
                                  startSec: 0,
                                  poster: d.preview_url,
                                })
                            : undefined
                        }
                      />
                    </td>
                    <td>{d.id}</td>
                    <td>
                      <JobStatus s={d.status} />
                    </td>
                    <td>{d.source_type}</td>
                    <td className="muted">
                      {d.index
                        ? `${d.index.overall_pct.toFixed(0)}% · sc=${d.index.scenes}`
                        : '—'}
                    </td>
                    <td>
                      <div>{d.title || '—'}</div>
                      <div className="muted" style={{ wordBreak: 'break-all' }}>
                        {d.file_path || d.url}
                      </div>
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {d.status === 'completed' && (
                        <>
                          <button
                            className="btn secondary"
                            type="button"
                            onClick={() =>
                              openPlayer({
                                downloadId: d.id,
                                title: `#${d.id} ${d.title || ''}`.trim(),
                                startSec: 0,
                                poster: d.preview_url,
                              })
                            }
                          >
                            ▶
                          </button>{' '}
                          <button
                            className="btn secondary"
                            type="button"
                            onClick={() => openDetail(d.id)}
                          >
                            scenes
                          </button>{' '}
                          <a
                            className="link"
                            href={api.fileUrl(d.id)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            open
                          </a>{' '}
                          <button
                            className="btn secondary"
                            type="button"
                            onClick={() =>
                              api.reindex(d.id).then(() => {
                                setMsg(`reindex queued for #${d.id}`)
                                setTab('queue')
                              })
                            }
                          >
                            reindex
                          </button>{' '}
                          <button
                            className="btn secondary"
                            type="button"
                            onClick={() => {
                              setSimilarId(String(d.id))
                              setTab('search')
                            }}
                          >
                            similar
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {tab === 'search' && (
        <section className="panel">
          <h2>Search</h2>
          <div className="row">
            <input
              className="grow"
              type="text"
              placeholder="テキスト検索…"
              value={searchQ}
              onChange={(e) => setSearchQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onSearchText()}
            />
            <button className="btn" type="button" disabled={busy} onClick={onSearchText}>
              Text search
            </button>
          </div>
          <div className="row">
            <input
              type="number"
              placeholder="download id"
              value={similarId}
              onChange={(e) => setSimilarId(e.target.value)}
              style={{ width: 120 }}
            />
            <button className="btn" type="button" disabled={busy} onClick={onSearchSimilar}>
              Similar (CLIP)
            </button>
          </div>
          {searchTitle && <p className="muted">{searchTitle}</p>}
          {!hits.length ? (
            <div className="empty">結果なし</div>
          ) : (
            <>
              <div className="hit-grid">
                {hits.map((h) => {
                  const src =
                    h.preview_url ||
                    (h.scene_index != null
                      ? api.keyframeUrl(h.download_id, h.scene_index)
                      : api.previewUrl(h.download_id))
                  const cap = `#${h.rank} dl=${h.download_id} sc=${h.scene_index ?? '—'} · ${h.score.toFixed(3)}`
                  const play = () =>
                    openPlayer({
                      downloadId: h.download_id,
                      title: h.title || `dl=${h.download_id} sc=${h.scene_index ?? '—'}`,
                      startSec: h.start_sec,
                      endSec: h.end_sec,
                      sceneIndex: h.scene_index,
                      poster: src,
                    })
                  return (
                    <div
                      className="scene-card"
                      key={`${h.rank}-${h.download_id}-${h.scene_index}`}
                      onClick={play}
                      title="このヒットから再生"
                    >
                      <img
                        src={src}
                        alt={cap}
                        loading="lazy"
                        onError={(e) => {
                          const el = e.target as HTMLImageElement
                          el.style.display = 'none'
                        }}
                      />
                      <div className="cap">
                        <strong>{h.score.toFixed(3)}</strong> · dl={h.download_id}
                        {h.scene_index != null ? ` sc=${h.scene_index}` : ''}
                        <br />
                        {h.start_sec != null && h.end_sec != null
                          ? `${h.start_sec.toFixed(1)}–${h.end_sec.toFixed(1)}s`
                          : h.title || '—'}
                      </div>
                    </div>
                  )
                })}
              </div>
              <table className="data" style={{ marginTop: '1rem' }}>
                <thead>
                  <tr>
                    <th>prev</th>
                    <th>#</th>
                    <th>score</th>
                    <th>dl</th>
                    <th>scene</th>
                    <th>time</th>
                    <th>title / path</th>
                  </tr>
                </thead>
                <tbody>
                  {hits.map((h) => {
                    const src =
                      h.preview_url ||
                      (h.scene_index != null
                        ? api.keyframeUrl(h.download_id, h.scene_index)
                        : api.previewUrl(h.download_id))
                    return (
                      <tr key={`row-${h.rank}-${h.download_id}-${h.scene_index}`}>
                        <td>
                          <Thumb
                            src={src}
                            alt={`hit ${h.rank}`}
                            size="sm"
                            caption={`dl=${h.download_id} sc=${h.scene_index ?? '—'}`}
                            onPlay={() =>
                              openPlayer({
                                downloadId: h.download_id,
                                title:
                                  h.title ||
                                  `dl=${h.download_id} sc=${h.scene_index ?? '—'}`,
                                startSec: h.start_sec,
                                endSec: h.end_sec,
                                sceneIndex: h.scene_index,
                                poster: src,
                              })
                            }
                          />
                        </td>
                        <td>{h.rank}</td>
                        <td className="hit-score">{h.score.toFixed(3)}</td>
                        <td>{h.download_id}</td>
                        <td>{h.scene_index ?? '—'}</td>
                        <td>
                          {h.start_sec != null && h.end_sec != null
                            ? `${h.start_sec.toFixed(1)}–${h.end_sec.toFixed(1)}s`
                            : '—'}
                        </td>
                        <td>
                          <div>{h.title || '—'}</div>
                          <div className="muted">{h.path || h.url}</div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </>
          )}
        </section>
      )}

      <FloatingPlayer state={player} onClose={closePlayer} />
    </div>
  )
}
