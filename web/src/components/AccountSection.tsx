import { useState, type FormEvent } from 'react'
import * as api from '../api'
import type { User } from '../types'

interface Props {
  self: User
  notify: (text: string, error?: boolean) => void
  onLoggedOut: () => void
}

export function AccountSection({ self, notify, onLoggedOut }: Props) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [busy, setBusy] = useState(false)

  const change = async (e: FormEvent) => {
    e.preventDefault()
    if (next.length < 8) { notify('New password: at least 8 characters', true); return }
    setBusy(true)
    try {
      await api.changePassword(current, next)
      notify('Password changed; other devices are logged out')
      setCurrent(''); setNext('')
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Could not change the password', true)
    } finally {
      setBusy(false)
    }
  }

  const logout = async (everywhere: boolean) => {
    try { await api.logout(everywhere) } catch { /* token already gone */ }
    api.setToken(null)
    onLoggedOut()
  }

  return (
    <div className="profile account">
      <div className="settings-section">Account</div>
      <div className="settings-hint">Logged in as <strong>{self.email}</strong>{self.is_admin ? ' (admin)' : ''}.</div>
      <form className="settings-row" onSubmit={change}>
        <input className="grow" type="password" placeholder="current password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
        <input className="grow" type="password" placeholder="new password (8+ characters)" autoComplete="new-password" required value={next} onChange={(e) => setNext(e.target.value)} />
        <button className="btn btn-sm" type="submit" disabled={busy}>Change password</button>
      </form>
      <div className="job-actions">
        <button className="btn btn-sm" onClick={() => logout(false)}>Log out</button>
        <button className="btn btn-sm" onClick={() => logout(true)}>Log out everywhere</button>
      </div>
    </div>
  )
}
