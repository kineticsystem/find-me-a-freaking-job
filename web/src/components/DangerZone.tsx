import { useState } from 'react'
import * as api from '../api'

interface Props {
  notify: (text: string, error?: boolean) => void
  onDone: () => void
}

type Action = 'jobs' | 'all'

const COPY: Record<Action, { button: string; title: string; body: string }> = {
  jobs: {
    button: 'Delete all jobs',
    title: 'Delete every job?',
    body: 'Every posting, its scores, your shortlist / applied / not-for-me decisions and their reasons, and the run history. Your sources, CV, notes and preferences stay. The next scan starts the search over.',
  },
  all: {
    button: 'Reset everything',
    title: 'Reset the whole database?',
    body: 'Everything above, plus every source — the ones discovered automatically and the ones you added — and what discovery has learned. Sources go back to the seed list. Your CV, notes and preferences are files, not database rows, and are not touched.',
  },
}

export function DangerZone({ notify, onDone }: Props) {
  const [pending, setPending] = useState<Action | null>(null)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)

  const cancel = () => { setPending(null); setTyped('') }

  const go = async () => {
    if (!pending || typed !== 'DELETE') return
    setBusy(true)
    try {
      if (pending === 'jobs') {
        const { deleted: d } = await api.resetJobs()
        notify(`Deleted ${d.jobs} jobs, ${d.evaluations} scores, ${d.decisions} decisions, ${d.runs} runs`)
      } else {
        const { deleted: d, sources_reseeded } = await api.resetAll()
        notify(`Database reset: ${d.jobs} jobs and ${d.sources} sources removed; ${sources_reseeded} seed sources restored`)
      }
      cancel()
      onDone()
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Failed', true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="profile danger">
      <div className="settings-section danger-title">Danger zone</div>
      <div className="settings-hint">These cannot be undone. Both ask you to type <code>DELETE</code> first, and are refused while a scan is running.</div>

      {pending === null ? (
        <div className="job-actions">
          <button className="btn btn-sm btn-danger-solid" onClick={() => setPending('jobs')}>{COPY.jobs.button}</button>
          <button className="btn btn-sm btn-danger-solid" onClick={() => setPending('all')}>{COPY.all.button}</button>
        </div>
      ) : (
        <div className="danger-confirm" role="alertdialog" aria-label={COPY[pending].title}>
          <div className="danger-confirm-title">{COPY[pending].title}</div>
          <div className="settings-hint">{COPY[pending].body}</div>
          <label className="settings-label" htmlFor="confirm-word">Type DELETE to confirm</label>
          <input id="confirm-word" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false}
                 onKeyDown={(e) => { if (e.key === 'Enter') go(); if (e.key === 'Escape') cancel() }} />
          <div className="job-actions">
            <button className="btn btn-sm btn-danger-solid" disabled={typed !== 'DELETE' || busy} onClick={go}>{busy ? 'Deleting…' : COPY[pending].button}</button>
            <button className="btn btn-sm" onClick={cancel} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}
