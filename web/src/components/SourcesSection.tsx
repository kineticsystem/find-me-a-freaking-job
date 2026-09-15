import { useEffect, useState } from 'react'
import * as api from '../api'
import type { Source } from '../types'

interface Props {
  notify: (text: string, error?: boolean) => void
}

const ORIGIN_LABEL: Record<Source['origin'], string> = { config: 'seed list', discovered: 'discovered', user: 'added by hand', keyword: 'search from your CV' }

const company = (s: Source) => (typeof s.config.company === 'string' && s.config.company) || (typeof s.config.slug === 'string' && s.config.slug) || s.id

export function SourcesSection({ notify }: Props) {
  const [sources, setSources] = useState<Source[] | null>(null)
  const [url, setUrl] = useState('')
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  const load = () => api.getSources().then(setSources).catch((e) => notify(e instanceof Error ? e.message : 'Could not load sources', true))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const onAdd = async () => {
    if (!url.trim()) return
    setAdding(true)
    try {
      const r = await api.addSource(url.trim())
      notify(r.open_positions == null ? `Added ${r.source_id}: ${r.note}` : `Added ${r.source_id} (${r.type}): ${r.open_positions} open positions, fetched on the next scan`)
      setUrl('')
      await load()
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not add', true)
    } finally {
      setAdding(false)
    }
  }

  const toggle = async (s: Source) => {
    setBusy(s.id)
    try {
      await api.setSourceEnabled(s.id, !s.following)
      setSources((list) => list?.map((x) => (x.id === s.id ? { ...x, following: s.following ? 0 : 1, enabled: s.following ? x.enabled : 1, fail_count: s.following ? x.fail_count : 0 } : x)) ?? null)
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not change', true)
    } finally {
      setBusy(null)
    }
  }

  const remove = async (s: Source) => {
    setBusy(s.id)
    try {
      await api.deleteSource(s.id)
      setSources((list) => list?.filter((x) => x.id !== s.id) ?? null)
      notify(`Removed ${s.id}; its jobs stay`)
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not remove', true)
    } finally {
      setBusy(null)
    }
  }

  // The ones you added yourself first, then by how much they yield. Not by
  // on/off: a row must stay put when its toggle is clicked.
  const rank: Record<Source['origin'], number> = { user: 0, keyword: 1, config: 2, discovered: 3 }
  const sorted = (sources ?? []).slice().sort((a, b) =>
    (rank[a.origin] - rank[b.origin]) || (b.jobs_stored - a.jobs_stored) || a.id.localeCompare(b.id))
  const shown = showAll ? sorted : sorted.slice(0, 12)
  const on = (sources ?? []).filter((s) => s.following).length

  return (
    <div className="profile sources">
      <div className="settings-section">Where it looks</div>
      <div className="settings-row">
        <input
          type="url" className="grow" placeholder="Paste a company careers URL (Greenhouse, Lever or Ashby)…"
          value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') onAdd() }}
          aria-label="careers URL"
        />
        <button className="btn btn-sm btn-primary" disabled={adding || !url.trim()} onClick={onAdd}>{adding ? 'Checking…' : 'Add'}</button>
      </div>
      <div className="settings-hint">
        e.g. <code>boards.greenhouse.io/stripe</code>, <code>jobs.lever.co/spotify</code>, <code>jobs.ashbyhq.com/linear</code>. The board is checked before it is added. Boards are also discovered automatically from the postings each scan finds. The list is yours: switching a source off hides its postings from you (not what you already shortlisted) and, if nobody else follows it, stops it being fetched.
      </div>

      {sources === null ? <div className="settings-hint">Loading…</div> : (
        <>
          <div className="settings-hint">You follow {on} of {sources.length} sources</div>
          <ul className="source-list">
            {shown.map((s) => (
              <li key={s.id} className={s.following ? '' : 'off'}>
                <label className="source-main">
                  <input type="checkbox" checked={!!s.following} disabled={busy === s.id} onChange={() => toggle(s)} />
                  <span className="source-name">{company(s)}</span>
                  <span className="chip">{s.type}</span>
                  <span className="chip" data-kind="origin">{ORIGIN_LABEL[s.origin]}</span>
                </label>
                <span className="source-meta">
                  {s.jobs_stored} stored
                  {s.last_error ? <span className="error"> · failing: {s.last_error.slice(0, 60)}</span> : ''}
                  {s.fail_count >= 5 ? <span className="error"> · paused after {s.fail_count} failures</span> : ''}
                </span>
                {s.deletable && <button className="btn btn-sm btn-danger" disabled={busy === s.id} onClick={() => remove(s)} title="Remove this source; its jobs stay">Remove</button>}
              </li>
            ))}
          </ul>
          {sorted.length > 12 && (
            <button className="btn btn-sm" onClick={() => setShowAll((v) => !v)}>{showAll ? 'Show fewer' : `Show all ${sorted.length}`}</button>
          )}
        </>
      )}
    </div>
  )
}
