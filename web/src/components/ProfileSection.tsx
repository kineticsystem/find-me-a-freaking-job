import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { Profile } from '../types'

interface Props {
  notify: (text: string, error?: boolean) => void
  onChanged?: () => void
}

const kb = (n: number) => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)

export function ProfileSection({ notify, onChanged }: Props) {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [notes, setNotes] = useState('')
  const [savingNotes, setSavingNotes] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const load = () =>
    api.getProfile().then((p) => { setProfile(p); setNotes(p.notes) })
      .catch((e) => notify(e instanceof Error ? e.message : 'Could not load profile', true))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const notesDirty = profile !== null && notes !== profile.notes

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const p = await api.uploadCv(file)
      setProfile(p); setNotes(p.notes)
      notify(`CV replaced with ${p.saved} — profile will be rebuilt and every job re-scored on the next scan`)
      onChanged?.()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Upload failed', true)
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  const onSaveNotes = async () => {
    setSavingNotes(true)
    try {
      const p = await api.saveNotes(notes)
      setProfile(p); setNotes(p.notes)
      notify('Notes saved — profile will be rebuilt and every job re-scored on the next scan')
      onChanged?.()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Could not save notes', true)
    } finally {
      setSavingNotes(false)
    }
  }

  return (
    <div className="profile">
      <div className="settings-section">Your profile</div>

      <div className="settings-label">CV</div>
      <div className="settings-hint">PDF, Markdown or text. Yours alone: kept in the database, the previous one replaced. Nothing leaves this machine.</div>
      <div className="settings-row">
        <span className="profile-cv">
          {profile === null ? 'Loading…' : profile.cv
            ? <><strong>{profile.cv.name}</strong> · {kb(profile.cv.bytes)} · {profile.cv_chars.toLocaleString()} characters read</>
            : <span className="error">No CV yet — upload one to get started.</span>}
        </span>
        <input ref={fileInput} type="file" accept=".pdf,.md,.txt,application/pdf,text/markdown,text/plain" hidden onChange={onPick} />
        <button className="btn btn-sm" disabled={uploading} onClick={() => fileInput.current?.click()}>
          {uploading ? 'Uploading…' : profile?.cv ? 'Replace CV' : 'Upload CV'}
        </button>
      </div>

      {profile?.digest && (
        <div className="settings-hint">How the model currently sees you: <em>{profile.digest.headline}</em>{profile.digest_current ? '' : ' (will be rebuilt on the next scan)'}</div>
      )}

      <label className="settings-label" htmlFor="notes">Your notes — what you want, in your own words</label>
      <div className="settings-hint">The model reads this, exactly as written, every time it judges a posting — alongside your CV and the rules in the preferences below. Put here everything that is a matter of judgement rather than a hard rule: what you want more of and less of, how your experience should be read, what to favour and what to be suspicious of, how you can be contracted. Be blunt; the model is told to be.</div>
      <textarea id="notes" rows={12} value={notes} onChange={(e) => setNotes(e.target.value)} spellCheck
                placeholder="e.g. I know ROS2 well but I am not a roboticist. Prefer product companies over consultancies. Remote only; I can invoice as a B2B contractor." />
      <div className="job-actions">
        <button className="btn btn-sm btn-primary" disabled={!notesDirty || savingNotes} onClick={onSaveNotes}>{savingNotes ? 'Saving…' : 'Save notes'}</button>
        {notesDirty && <button className="btn btn-sm" onClick={() => setNotes(profile?.notes ?? '')}>Discard</button>}
      </div>
    </div>
  )
}
