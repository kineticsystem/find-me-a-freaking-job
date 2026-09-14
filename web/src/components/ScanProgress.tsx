import type { Progress } from '../types'

const STAGE_LABEL: Record<string, string> = {
  starting: 'Starting',
  profile: 'Reading your CV and notes',
  fetch: 'Fetching postings',
  extract: 'Structuring free-text adverts',
  store: 'Storing new postings',
  triage: 'Scoring',
  deepdive: 'Reading the best matches in full',
  discover: 'Looking for new company boards',
}

function minutes(s: number): string {
  if (s < 60) return 'under a minute'
  const m = Math.round(s / 60)
  return `about ${m} minute${m === 1 ? '' : 's'}`
}

export function ScanProgress({ p }: { p: Progress }) {
  if (!p.active) return <div className="progress"><span className="progress-text">Scan starting…</span></div>
  const label = STAGE_LABEL[p.stage ?? ''] ?? p.stage
  const hasUnits = (p.total ?? 0) > 0
  const pct = hasUnits ? Math.round(((p.current ?? 0) / (p.total ?? 1)) * 100) : null
  const parts: string[] = [label]
  if (p.stage === 'fetch' && p.fetched) parts.push(`${p.fetched.toLocaleString()} postings from ${p.sources} sources`)
  if (hasUnits) parts.push(`${p.current} of ${p.total}${p.stage === 'triage' ? ' batches' : ''}`)
  if (hasUnits && p.eta_seconds != null) parts.push(`${minutes(p.eta_seconds)} left in this step`)
  if (p.elapsed_seconds != null) parts.push(`running for ${minutes(p.elapsed_seconds).replace('about ', '')}`)

  return (
    <div className="progress" role="status" aria-live="polite">
      <div className="progress-bar"><div className={`progress-fill${pct === null ? ' indeterminate' : ''}`} style={pct === null ? undefined : { width: `${pct}%` }} /></div>
      <div className="progress-text">{parts.join(' · ')}</div>
    </div>
  )
}
