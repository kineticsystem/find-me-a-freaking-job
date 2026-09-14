import { useEffect, useState } from 'react'
import * as api from '../api'
import type { Settings } from '../types'
import { ProfileSection } from './ProfileSection'
import { SourcesSection } from './SourcesSection'
import { PreferencesSection } from './PreferencesSection'
import { DangerZone } from './DangerZone'
import { UNIT_MINUTES, describe, split, type Unit } from '../interval'
import { getTheme, setTheme, type Theme } from '../theme'

interface Props {
  open: boolean
  onClose: () => void
  onSaved: (s: Settings) => void
  onChanged?: () => void   // anything in the panel changed: profile, preferences, sources
  notify: (text: string, error?: boolean) => void
}


export function SettingsPanel({ open, onClose, onSaved, onChanged, notify }: Props) {
  const [current, setCurrent] = useState<Settings | null>(null)
  const [n, setN] = useState(2)
  const [unit, setUnit] = useState<Unit>('hours')
  const [runOnStart, setRunOnStart] = useState(true)
  const [saving, setSaving] = useState(false)
  const [theme, setThemeState] = useState<Theme>(getTheme)
  const chooseTheme = (t: Theme) => { setTheme(t); setThemeState(t) }

  useEffect(() => {
    if (!open) return
    api.getSettings().then((s) => {
      setCurrent(s)
      const { n, unit } = split(s.interval_minutes)
      setN(n); setUnit(unit); setRunOnStart(s.run_on_start)
    }).catch((e) => notify(e instanceof Error ? e.message : 'Could not load settings', true))
  }, [open, notify])

  if (!open) return null

  const minutes = Math.round(n * UNIT_MINUTES[unit])
  const valid = minutes >= 5 && minutes <= 7 * 1440
  const dirty = current !== null && (minutes !== current.interval_minutes || runOnStart !== current.run_on_start)

  const save = async () => {
    setSaving(true)
    try {
      const s = await api.updateSettings({ interval_minutes: minutes, run_on_start: runOnStart })
      setCurrent(s)
      onSaved(s)
      notify(`Scanning every ${describe(s.interval_minutes)} — saved, no restart needed`)
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Could not save', true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="settings" role="dialog" aria-label="Settings">
      <div className="settings-section">Appearance</div>
      <div className="settings-row" role="radiogroup" aria-label="Theme">
        <span className="settings-label" style={{ marginTop: 0 }}>Theme</span>
        {(['auto', 'light', 'dark'] as Theme[]).map((t) => (
          <button key={t} type="button" role="radio" aria-checked={theme === t}
                  className={`chip chip-btn${theme === t ? ' on' : ''}`} onClick={() => chooseTheme(t)}>
            {t === 'auto' ? 'Auto' : t === 'light' ? 'Light' : 'Dark'}
          </button>
        ))}
      </div>
      <div className="settings-hint">Remembered by this browser only, so your phone and your desktop can differ.</div>

      <div className="settings-section">Scanning</div>
      <div className="settings-row">
        <label htmlFor="interval-n">Scan for jobs every</label>
        <input id="interval-n" type="number" min={1} step={1} value={n}
               onChange={(e) => setN(Math.max(1, Math.floor(Number(e.target.value) || 1)))} />
        <select value={unit} onChange={(e) => setUnit(e.target.value as Unit)} aria-label="unit">
          <option value="minutes">minutes</option>
          <option value="hours">hours</option>
          <option value="days">days</option>
        </select>
      </div>
      {!valid && <div className="settings-hint error">Between 5 minutes and 7 days.</div>}
      <label className="settings-row">
        <input type="checkbox" checked={runOnStart} onChange={(e) => setRunOnStart(e.target.checked)} />
        Run a scan as soon as the server starts
      </label>
      <div className="settings-hint">
        {current?.next_run ? `Next scan: ${new Date(current.next_run).toLocaleString()}` : ''}
        {current?.running ? ' · a scan is running now' : ''}
      </div>
      <div className="job-actions">
        <button className="btn btn-sm btn-primary" disabled={!valid || !dirty || saving} onClick={save}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>

      <ProfileSection notify={notify} onChanged={onChanged} />
      <PreferencesSection notify={notify} onChanged={onChanged} />
      <SourcesSection notify={notify} />
      <DangerZone notify={notify} onDone={() => { onSaved(current as Settings); onClose() }} />

      <div className="job-actions">
        <button className="btn btn-sm" onClick={onClose}>Close</button>
      </div>
    </div>
  )
}
