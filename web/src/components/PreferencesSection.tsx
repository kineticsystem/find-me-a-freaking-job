import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { LocationRule, Preferences } from '../types'
import { CURRENCIES, SENIORITIES } from '../types'
import { ChipInput } from './ChipInput'

interface Props {
  notify: (text: string, error?: boolean) => void
  onChanged?: () => void
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

export function PreferencesSection({ notify, onChanged }: Props) {
  const [saved, setSaved] = useState<Preferences | null>(null)
  const [p, setP] = useState<Preferences | null>(null)
  const [saving, setSaving] = useState(false)
  // The latest form state, readable from the click handler even when the
  // click that triggers it is the same gesture that blurred a chip input and
  // committed its text (the blur's state update lands before the click).
  const pRef = useRef<Preferences | null>(null)
  useEffect(() => { pRef.current = p }, [p])

  const load = () => api.getPreferences().then((r) => { setSaved(r.preferences); setP(r.preferences) })
    .catch((e) => notify(e instanceof Error ? e.message : 'Could not load preferences', true))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (!p || !saved) return <div className="profile"><div className="settings-section">Your preferences</div><div className="settings-hint">Loading…</div></div>

  const dirty = !same(p, saved)
  const set = <K extends keyof Preferences>(k: K, v: Preferences[K]) => setP({ ...p, [k]: v })
  const setRule = (i: number, patch: Partial<LocationRule>) => set('location_rules', p.location_rules.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const moveRule = (i: number, d: -1 | 1) => {
    const rules = p.location_rules.slice(); const j = i + d
    if (j < 0 || j >= rules.length) return
    ;[rules[i], rules[j]] = [rules[j], rules[i]]; set('location_rules', rules)
  }

  const save = async () => {
    const current = pRef.current
    if (!current) return
    if (same(current, saved)) { notify('Nothing to save'); return }
    setSaving(true)
    try {
      const r = await api.savePreferences(current)
      setSaved(r.preferences); setP(r.preferences)
      notify('Preferences saved — every job will be re-scored on the next scan')
      onChanged?.()
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not save', true)
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="profile prefs">
      <div className="settings-section">Your preferences</div>
      <div className="settings-hint">Facts and hard rules the model applies to every posting. Anything here changes the scoring, so every job is re-scored on the next scan after a save.</div>

      <div className="prefs-grid">
        <label>Based in <input value={p.based_in} onChange={(e) => set('based_in', e.target.value)} placeholder="country" /></label>
        <label>Citizenship <ChipInput values={p.citizenship} onChange={(v) => set('citizenship', v)} placeholder="e.g. Germany, EU…" /></label>
        <label>Languages <ChipInput values={p.languages} onChange={(v) => set('languages', v)} placeholder="English…" /></label>
      </div>

      <label className="settings-label" htmlFor="auth">Work authorisation — what you can legally take, and from where</label>
      <textarea id="auth" rows={4} value={p.work_authorization_notes} onChange={(e) => set('work_authorization_notes', e.target.value)}
                placeholder="e.g. EU citizen; can invoice as a B2B contractor or work via an EOR; no US work authorisation." />

      <label className="settings-label">Titles you are after</label>
      <ChipInput values={p.titles} onChange={(v) => set('titles', v)} placeholder="Senior Software Engineer, Platform Engineer…" />

      <label className="settings-label">Seniority</label>
      <div className="chips">
        {SENIORITIES.map((s) => (
          <button key={s} type="button" className={`chip chip-btn${p.seniority.includes(s) ? ' on' : ''}`}
                  onClick={() => set('seniority', p.seniority.includes(s) ? p.seniority.filter((x) => x !== s) : [...p.seniority, s])}>{s}</button>
        ))}
      </div>

      <div className="prefs-grid">
        <label>Must have <ChipInput values={p.must_have} onChange={(v) => set('must_have', v)} placeholder="C++…" /></label>
        <label>Nice to have <ChipInput values={p.nice_to_have} onChange={(v) => set('nice_to_have', v)} placeholder="ROS2, Python…" /></label>
      </div>

      <label className="settings-label">Dealbreakers</label>
      <div className="settings-hint">Things you will not accept, in plain words. If a posting asks for any of them, the model treats it as a no: the score is capped at 20 no matter how good the rest of the fit, so it drops to the bottom of the list and never reaches the digest. The posting is not deleted — you can still find it with a low minimum score — it just cannot rank.</div>
      <ChipInput values={p.dealbreakers} onChange={(v) => set('dealbreakers', v)} placeholder="security clearance, relocation required, on-site 5 days a week…" />

      <label className="settings-label">Salary floor</label>
      <div className="settings-row">
        <label className="inline"><input type="checkbox" checked={p.min_salary !== null}
               onChange={(e) => set('min_salary', e.target.checked ? { amount: 60000, currency: 'EUR', period: 'year' } : null)} /> at least</label>
        {p.min_salary && (
          <>
            <input type="number" min={0} step={1000} value={p.min_salary.amount} aria-label="amount"
                   onChange={(e) => set('min_salary', { ...p.min_salary!, amount: Math.max(0, Number(e.target.value) || 0) })} />
            <select value={p.min_salary.currency} aria-label="currency" onChange={(e) => set('min_salary', { ...p.min_salary!, currency: e.target.value })}>
              {CURRENCIES.map((c) => <option key={c}>{c}</option>)}
            </select>
            <select value={p.min_salary.period} aria-label="period" onChange={(e) => set('min_salary', { ...p.min_salary!, period: e.target.value as 'year' })}>
              <option value="year">per year</option><option value="month">per month</option><option value="day">per day</option><option value="hour">per hour</option>
            </select>
          </>
        )}
      </div>

      <label className="settings-label">Markets, in the order to search them — the first wins ties; later ones still surface</label>
      <ul className="rules">
        {p.location_rules.map((r, i) => (
          <li key={i}>
            <span className="rule-n">{i + 1}.</span>
            <input value={r.country ?? r.region ?? ''} aria-label="market" placeholder="country or region"
                   onChange={(e) => setRule(i, { country: e.target.value, region: null })} />
            <select value={r.remote} aria-label="remote" onChange={(e) => setRule(i, { remote: e.target.value as LocationRule['remote'] })}>
              <option value="required">remote only</option><option value="any">remote or on-site</option><option value="no">on-site only</option>
            </select>
            <input className="grow" value={r.note ?? ''} placeholder="note for the model (optional)" aria-label="note"
                   onChange={(e) => setRule(i, { note: e.target.value || null })} />
            <span className="rule-btns">
              <button type="button" className="btn btn-sm" disabled={i === 0} onClick={() => moveRule(i, -1)} aria-label="move up">↑</button>
              <button type="button" className="btn btn-sm" disabled={i === p.location_rules.length - 1} onClick={() => moveRule(i, 1)} aria-label="move down">↓</button>
              <button type="button" className="btn btn-sm btn-danger" onClick={() => set('location_rules', p.location_rules.filter((_, j) => j !== i))} aria-label="remove market">✕</button>
            </span>
          </li>
        ))}
      </ul>
      <div><button type="button" className="btn btn-sm" onClick={() => set('location_rules', [...p.location_rules, { country: '', remote: 'required', note: null }])}>+ Add a market</button></div>

      <div className="job-actions">
        {/* Never disabled for "not dirty": a click on a disabled button is
            swallowed, and the click that commits a half-typed chip must
            still reach us. */}
        <button className="btn btn-sm btn-primary" disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save preferences'}</button>
        {dirty && <button className="btn btn-sm" onClick={() => setP(saved)}>Discard</button>}
      </div>

    </div>
  )
}
