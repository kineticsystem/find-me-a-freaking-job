import { useState } from 'react'
import type { Job, Status } from '../types'
import { DISMISS_REASONS } from '../types'

interface Props {
  job: Job
  busy: boolean
  onStatus: (job: Job, status: Status, reason?: string) => void
  onDelete: (job: Job) => void
}

function band(score: number | null): 'strong' | 'maybe' | 'weak' | 'none' {
  if (score === null) return 'none'
  if (score >= 70) return 'strong'
  if (score >= 45) return 'maybe'
  return 'weak'
}

function ago(iso: string): string {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 30) return `${days}d ago`
  return `${Math.floor(days / 30)}mo ago`
}

export function JobCard({ job, busy, onStatus, onDelete }: Props) {
  const [confirming, setConfirming] = useState(false)
  const [dismissing, setDismissing] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const [freeText, setFreeText] = useState('')
  const link = job.apply_url || job.url || undefined
  const salary = job.salary || job.salary_raw
  const archived = job.status === 'archived'
  const dismissed = job.status === 'dismissed'

  const reason = [...picked, freeText.trim()].filter(Boolean).join(', ')
  const submitDismiss = () => {
    setDismissing(false)
    onStatus(job, 'dismissed', reason || undefined)
    setPicked([]); setFreeText('')
  }
  const toggle = (r: string) => setPicked((p) => (p.includes(r) ? p.filter((x) => x !== r) : [...p, r]))

  return (
    <article className={`job${archived || dismissed ? ' is-archived' : ''}`} aria-busy={busy}>
      <div className="job-head">
        <div className="score" data-band={band(job.score)} title={job.verdict ?? 'not yet evaluated'}>
          {job.score ?? '–'}
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <h3 className="job-title">
            {link ? <a href={link} target="_blank" rel="noreferrer noopener">{job.title}</a> : job.title}
          </h3>
          <div className="job-company">{job.company}{job.location ? ` · ${job.location}` : ''}</div>
        </div>
      </div>

      <div className="chips">
        {job.status !== 'new' && <span className="chip" data-kind="status" data-value={job.status}>{job.status}</span>}
        {job.remote_type && job.remote_type !== 'unknown' && <span className="chip" data-kind="remote">{job.remote_type}</span>}
        {salary && <span className="chip">{salary}</span>}
        {job.eligible === 0 && <span className="chip" data-kind="warn">not eligible</span>}
        {(job.tech_stack ?? []).slice(0, 5).map((t) => <span className="chip" key={t}>{t}</span>)}
      </div>

      {job.summary && <p className="job-summary">{job.summary}</p>}
      {!job.summary && job.rationale && <p className="job-summary">{job.rationale}</p>}
      {(job.concerns ?? []).length > 0 && (
        <div className="job-meta">⚠ {job.concerns.join(' · ')}</div>
      )}

      <div className="job-meta">
        {job.source_id} · found {ago(job.first_seen)}
        {job.seen_count > 1 ? ` · seen ${job.seen_count}×` : ''}
        {job.notes ? ` · note: ${job.notes}` : ''}
      </div>
      {dismissed && job.reason && <div className="job-meta reason">✕ {job.reason}</div>}

      {job.description && (
        <details className="more">
          <summary>Full posting</summary>
          <div className="job-desc">{job.description}</div>
        </details>
      )}

      {dismissing ? (
        <div className="dismiss" role="group" aria-label="Why not?">
          <div className="dismiss-title">Why not? The model will learn from it.</div>
          <div className="chips">
            {DISMISS_REASONS.map((r) => (
              <button key={r} type="button" className={`chip chip-btn${picked.includes(r) ? ' on' : ''}`} onClick={() => toggle(r)}>{r}</button>
            ))}
          </div>
          <input
            type="text" placeholder="anything else… (optional)" value={freeText} maxLength={200}
            onChange={(e) => setFreeText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submitDismiss() }}
          />
          <div className="job-actions">
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={submitDismiss}>Not for me</button>
            <button className="btn btn-sm" onClick={() => { setDismissing(false); setPicked([]); setFreeText('') }}>Cancel</button>
          </div>
        </div>
      ) : (
      <div className="job-actions">
        {job.status !== 'shortlisted' && job.status !== 'applied' && !dismissed && (
          <button className="btn btn-sm" disabled={busy} onClick={() => onStatus(job, 'shortlisted')}>★ Shortlist</button>
        )}
        {job.status === 'shortlisted' && (
          <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => onStatus(job, 'applied')}>✓ Applied</button>
        )}
        {!archived && !dismissed && (
          <button className="btn btn-sm" disabled={busy} onClick={() => setDismissing(true)}>✕ Not for me</button>
        )}
        {!archived && !dismissed && (
          <button className="btn btn-sm" disabled={busy} onClick={() => onStatus(job, 'archived')}>Archive</button>
        )}
        {archived && (
          <button className="btn btn-sm" disabled={busy} onClick={() => onStatus(job, 'new')}>Unarchive</button>
        )}
        {dismissed && (
          <button className="btn btn-sm" disabled={busy} onClick={() => onStatus(job, 'new')}>Restore</button>
        )}
        {job.status !== 'new' && !archived && !dismissed && (
          <button className="btn btn-sm" disabled={busy} onClick={() => onStatus(job, 'new')}>Reset</button>
        )}
        {confirming ? (
          <span className="confirm">
            Delete forever?
            <button className="btn btn-sm btn-danger" disabled={busy} onClick={() => { setConfirming(false); onDelete(job) }}>Yes</button>
            <button className="btn btn-sm" onClick={() => setConfirming(false)}>No</button>
          </span>
        ) : (
          <button className="btn btn-sm btn-danger" disabled={busy} onClick={() => setConfirming(true)}>Delete</button>
        )}
      </div>
      )}
    </article>
  )
}
