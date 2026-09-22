import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api'
import { Filters } from './components/Filters'
import { JobCard } from './components/JobCard'
import { SettingsPanel } from './components/SettingsPanel'
import { Login } from './components/Login'
import { describe, duration } from './interval'
import { ScanProgress } from './components/ScanProgress'
import type { Facets, Health, Job, JobQuery, Status, User } from './types'
import { DEFAULT_QUERY } from './types'

interface Toast { text: string; error?: boolean }

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** Gate: who is logged in. `undefined` while the stored token is being checked. */
export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined)
  const [needsSetup, setNeedsSetup] = useState(false)

  useEffect(() => {
    api.setUnauthorizedHandler(() => setUser(null))
    const check = api.getToken() ? api.me().then(setUser).catch(() => setUser(null)) : Promise.resolve().then(() => setUser(null))
    check.then(() => api.getHealth()).then((h) => setNeedsSetup(h.needs_setup)).catch(() => {})
    return () => api.setUnauthorizedHandler(null)
  }, [])

  if (user === undefined) return null
  if (!user) return <Login firstUser={needsSetup} onLoggedIn={(u) => { setNeedsSetup(false); setUser(u) }} />
  return <Workspace user={user} onLoggedOut={() => setUser(null)} />
}


function Workspace({ user, onLoggedOut }: { user: User; onLoggedOut: () => void }) {
  const [query, setQuery] = useState<JobQuery>(DEFAULT_QUERY)
  const debouncedQ = useDebounced(query.q, 250)
  const effective: JobQuery = { ...query, q: debouncedQ }

  const [jobs, setJobs] = useState<Job[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Set<number>>(new Set())
  const [facets, setFacets] = useState<Facets | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [toast, setToast] = useState<Toast | null>(null)
  const [archiveDays, setArchiveDays] = useState(30)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const reqId = useRef(0)

  // One dismiss timer at a time: a new toast cancels the previous one's, or
  // the old timer would wipe the new message moments after it appeared.
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), error ? 5000 : 2500)
  }, [])

  const refreshMeta = useCallback(() => {
    api.getFacets().then(setFacets).catch(() => undefined)
    api.getHealth().then(setHealth).catch(() => undefined)
  }, [])

  const load = useCallback(async (q: JobQuery, offset: number) => {
    const id = ++reqId.current
    setLoading(true)
    setError(null)
    try {
      const page = await api.listJobs(q, offset)
      if (id !== reqId.current) return // a newer request superseded this one
      setJobs((prev) => (offset === 0 ? page.jobs : [...prev, ...page.jobs]))
      setTotal(page.total)
    } catch (e) {
      if (id !== reqId.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (id === reqId.current) setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(effective, 0) }, [
    effective.q, effective.status, effective.remote, effective.source,
    effective.minScore, effective.sort, effective.hidden, load,
  ])
  useEffect(refreshMeta, [refreshMeta])
  // While a scan runs, keep the progress line current and pick up new scores.
  useEffect(() => {
    if (!health?.running) return
    const t = setInterval(() => {
      api.getHealth().then((h) => {
        setHealth(h)
        if (!h.running) { refreshMeta(); load(effective, 0) }   // it just finished: show the results
      }).catch(() => undefined)
    }, 3000)
    return () => clearInterval(t)
  }, [health?.running, refreshMeta, load, effective]) // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (p: Partial<JobQuery>) => setQuery((q) => ({ ...q, ...p }))

  const withBusy = async (id: number, fn: () => Promise<void>) => {
    setBusy((b) => new Set(b).add(id))
    try {
      await fn()
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Request failed', true)
    } finally {
      setBusy((b) => { const n = new Set(b); n.delete(id); return n })
    }
  }

  const stillVisible = (status: Status): boolean => {
    if (effective.status) return status === effective.status
    const hiddenStatus = status === 'archived' || status === 'dismissed'
    return effective.hidden ? hiddenStatus : !hiddenStatus
  }

  const onStatus = (job: Job, status: Status, reason?: string) =>
    withBusy(job.id, async () => {
      await api.setStatus(job.id, status, reason ? { reason } : {})
      if (stillVisible(status)) {
        setJobs((js) => js.map((j) => (j.id === job.id ? { ...j, status, reason: status === 'dismissed' ? reason ?? null : null } : j)))
      } else {
        setJobs((js) => js.filter((j) => j.id !== job.id))
        setTotal((t) => t - 1)
      }
      notify(status === 'new' ? 'Restored' : status === 'dismissed' ? (reason ? 'Not for me — the model will remember' : 'Not for me') : `${status[0].toUpperCase()}${status.slice(1)}`)
      refreshMeta()
    })

  const onDelete = (job: Job) =>
    withBusy(job.id, async () => {
      await api.deleteJob(job.id)
      setJobs((js) => js.filter((j) => j.id !== job.id))
      setTotal((t) => t - 1)
      notify('Deleted')
      refreshMeta()
    })

  const onArchiveOld = async () => {
    try {
      const { archived } = await api.archiveOlderThan(archiveDays)
      notify(archived ? `Archived ${archived} job${archived === 1 ? '' : 's'}` : 'Nothing that old')
      if (archived) { load(effective, 0); refreshMeta() }
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Request failed', true)
    }
  }

  const onRunNow = async () => {
    try {
      await api.triggerRun()
      notify('Scan queued — results will appear as it progresses')
      setTimeout(refreshMeta, 1500)
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not start a run', true)
    }
  }

  const onStop = async () => {
    try {
      await api.stopRun()
      notify('Stopping — finishing nothing further; what is scored so far stays')
      setTimeout(refreshMeta, 500)
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not stop', true)
    }
  }

  const nextRun = health?.next_run ? new Date(health.next_run) : null

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-row">
          <div className="brand">
            <div>🎯 Find Me a Freaking Job<small>{health?.stats ? `${health.stats.jobs} stored` : ''}</small></div>
            <a className="brand-link" href="http://www.findmeafreakingjob.com" target="_blank" rel="noreferrer noopener">www.findmeafreakingjob.com</a>
          </div>
          <div className="search">
            <input
              type="search" placeholder="Search title, company, location…"
              value={query.q} onChange={(e) => patch({ q: e.target.value })}
              aria-label="Search"
            />
          </div>
          <button className="btn filter-toggle" onClick={() => setFiltersOpen((o) => !o)} aria-expanded={filtersOpen}>
            Filters
          </button>
          <button className="btn btn-icon" onClick={() => setSettingsOpen((o) => !o)} aria-expanded={settingsOpen} aria-label="Settings" title="Settings">⚙</button>
        </div>
      </header>

      <main className="main">
        {health?.setup && !health.setup.ready && (
          <div className="banner setup" role="status">
            <strong>Complete your <button className="linkish" onClick={() => setSettingsOpen(true)}>⚙ Settings</button> before your first scan:</strong>
            <ul>
              <li className={health.setup.cv ? 'done' : ''}>{health.setup.cv ? '✓' : '○'} Upload your CV</li>
              <li className={health.setup.notes ? 'done' : ''}>{health.setup.notes ? '✓' : '○'} Write your notes — what you want, in your own words</li>
              <li className={health.setup.preferences ? 'done' : ''}>{health.setup.preferences ? '✓' : '○'} Set where you are based and the titles you are after</li>
            </ul>
          </div>
        )}
        <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} onSaved={() => { refreshMeta(); load(effective, 0) }} onChanged={refreshMeta} notify={notify} self={user} onLoggedOut={onLoggedOut} />
        <Filters query={query} facets={facets} open={filtersOpen} onChange={patch} onReset={() => setQuery({ ...DEFAULT_QUERY, q: query.q })} />

        <div className="summary">
          <span>{loading && jobs.length === 0 ? 'Loading…' : `${total} job${total === 1 ? '' : 's'}`}</span>
          {health && (
            <span>
              {(!health.running && nextRun ? `next scan ${nextRun.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ` : '') + `every ${describe(health.interval_minutes)}`}
              {health.last_run?.duration_seconds != null && (
                <span className="last-run" title={`Scan #${health.last_run.id}, ${health.last_run.status}: ${health.last_run.new} new postings, ${health.last_run.triaged} scored, ${health.last_run.deepdived} deep dives, finished ${new Date(health.last_run.finished_at).toLocaleString()}`}>
                  {` · last scan took ${duration(health.last_run.duration_seconds)}`}{health.last_run.status !== 'ok' ? ` (${health.last_run.status})` : ''}
                </span>
              )}
            </span>
          )}
          {health?.setup && health.stale_scores != null && health.stale_scores > 0 && (
            <span title="Faded scores are from before your last CV, notes or preferences change">
              {health.stale_scores} score{health.stale_scores === 1 ? '' : 's'} from before your last change
              {health.running ? ' · re-scoring now' : ' · re-scored on the next scan'}
            </span>
          )}
          <span className="spacer" style={{ flex: 1 }} />
          <span className="archive-old">
            <button className="btn btn-sm" onClick={onArchiveOld} title="Archive 'new' jobs not seen for this many days">Archive older than</button>
            <input type="number" min={0} value={archiveDays} onChange={(e) => setArchiveDays(Math.max(0, Number(e.target.value) || 0))} aria-label="days" />
            <span>days</span>
          </span>
        </div>

        <div className="scan-row">
          {health?.running ? (
            user.is_admin ? (
              <button className="btn btn-scan btn-danger" onClick={onStop} disabled={health.progress.stopping}>
                {health.progress.stopping ? 'Stopping…' : '■ Stop scan'}
              </button>
            ) : <span className="btn-note">a scan is running</span>
          ) : (
            user.is_admin && <button className="btn btn-primary btn-scan" onClick={onRunNow} disabled={!health?.setup?.ready}
                                     title="Fetch every source, then score every posting that has no score yet — hours the first time, minutes once caught up">▶ Scan now</button>
          )}
          {user.is_admin && health?.setup && !health.running && !health.setup.ready && <span className="btn-note">complete your settings first</span>}
        </div>
        {health?.running && <ScanProgress p={health.progress} />}

        {error && <div className="empty">Could not load jobs: {error}</div>}
        {!error && !loading && jobs.length === 0 && <div className="empty">Nothing matches. Try clearing the filters.</div>}

        <div className="jobs">
          {jobs.map((job) => (
            <JobCard key={job.id} job={job} busy={busy.has(job.id)} onStatus={onStatus} onDelete={onDelete} />
          ))}
        </div>

        {jobs.length < total && (
          <div className="loadmore">
            <button className="btn" disabled={loading} onClick={() => load(effective, jobs.length)}>
              {loading ? 'Loading…' : `Load more (${total - jobs.length} left)`}
            </button>
          </div>
        )}
      </main>

      {toast && <div className={`toast${toast.error ? ' error' : ''}`} role="status">{toast.text}</div>}
    </div>
  )
}
