import type { Facets, JobQuery, RemoteType, Sort, Status } from '../types'
import { REMOTE_TYPES, STATUSES } from '../types'

interface Props {
  query: JobQuery
  facets: Facets | null
  open: boolean
  onChange: (patch: Partial<JobQuery>) => void
  onReset: () => void
}

const count = (m: Record<string, number> | undefined, k: string) => (m?.[k] ? ` (${m[k]})` : '')

export function Filters({ query, facets, open, onChange, onReset }: Props) {
  const sources = Object.keys(facets?.source ?? {})
  const active =
    query.status || query.remote || query.source || query.minScore > 0 || query.hidden

  return (
    <div className={`filters${open ? ' open' : ''}`} role="group" aria-label="Filters">
      <label>
        Status
        <select value={query.status} onChange={(e) => onChange({ status: e.target.value as Status | '' })}>
          <option value="">Active</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s}{count(facets?.status, s)}</option>
          ))}
        </select>
      </label>
      <label>
        Remote
        <select value={query.remote} onChange={(e) => onChange({ remote: e.target.value as RemoteType | '' })}>
          <option value="">Any</option>
          {REMOTE_TYPES.map((r) => (
            <option key={r} value={r}>{r}{count(facets?.remote, r)}</option>
          ))}
        </select>
      </label>
      <label>
        Source
        <select value={query.source} onChange={(e) => onChange({ source: e.target.value })}>
          <option value="">All</option>
          {sources.map((s) => (
            <option key={s} value={s}>{s}{count(facets?.source, s)}</option>
          ))}
        </select>
      </label>
      <label>
        Min score
        <input
          type="number" min={0} max={100} step={5} value={query.minScore}
          onChange={(e) => onChange({ minScore: Math.max(0, Math.min(100, Number(e.target.value) || 0)) })}
        />
      </label>
      <label>
        Sort
        <select value={query.sort} onChange={(e) => onChange({ sort: e.target.value as Sort })}>
          <option value="score">Best match</option>
          <option value="newest">Newest</option>
          <option value="company">Company A–Z</option>
        </select>
      </label>
      {!query.status && (
        <label>
          <input type="checkbox" checked={query.hidden} onChange={(e) => onChange({ hidden: e.target.checked })} />
          Show only archived and dismissed
        </label>
      )}
      <span className="spacer" />
      {active && <button className="btn btn-sm" onClick={onReset}>Clear filters</button>}
    </div>
  )
}
