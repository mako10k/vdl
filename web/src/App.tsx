import { useCallback, useEffect, useState } from 'react'
import './App.css'
import {
  api,
  type Candidate,
  type Download,
  type Job,
  type SearchHit,
} from './api'

type Tab = 'get' | 'queue' | 'registry' | 'search' | 'candidates'

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

  const [candUrl, setCandUrl] = useState('')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [candPage, setCandPage] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())

  const [searchQ, setSearchQ] = useState('')
  const [similarId, setSimilarId] = useState('')
  const [hits, setHits] = useState<SearchHit[]>([])
  const [searchTitle, setSearchTitle] = useState('')

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

  useEffect(() => {
    refreshStatus()
    const t = setInterval(refreshStatus, 3000)
    return () => clearInterval(t)
  }, [refreshStatus])

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
      setSelected(
        new Set(r.candidates.filter((c) => !c.downloaded).map((c) => c.index)),
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
            onClick={() => setTab(id)}
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
          <h2>HTML candidates</h2>
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
              {candidates.map((c) => (
                <div className="cand" key={c.index}>
                  <label>
                    <input
                      type="checkbox"
                      checked={selected.has(c.index)}
                      disabled={c.downloaded}
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
                        {c.downloaded
                          ? `済 id=${c.download_id}`
                          : '新規'}{' '}
                        · {c.source}
                        {c.title ? ` · ${c.title}` : ''}
                      </div>
                    </div>
                  </label>
                </div>
              ))}
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
          {!downloads.length ? (
            <div className="empty">ダウンロードなし</div>
          ) : (
            <table className="data">
              <thead>
                <tr>
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
                  <tr key={d.id}>
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
                    <td>
                      {d.status === 'completed' && (
                        <>
                          <a className="link" href={api.fileUrl(d.id)} target="_blank" rel="noreferrer">
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
            <table className="data">
              <thead>
                <tr>
                  <th>#</th>
                  <th>score</th>
                  <th>dl</th>
                  <th>scene</th>
                  <th>time</th>
                  <th>title / path</th>
                </tr>
              </thead>
              <tbody>
                {hits.map((h) => (
                  <tr key={`${h.rank}-${h.download_id}-${h.scene_index}`}>
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
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  )
}
