import { useEffect, useState, type FormEvent } from 'react'
import * as api from '../api'
import type { User } from '../types'

interface Props {
  self: User
  notify: (text: string, error?: boolean) => void
}

/** Admin only: who can log in. Passwords are typed once here and never shown again. */
export function UsersSection({ self, notify }: Props) {
  const [users, setUsers] = useState<User[]>([])
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [busy, setBusy] = useState(false)
  const [resetting, setResetting] = useState<number | null>(null)
  const [newPassword, setNewPassword] = useState('')

  const reload = () => api.listUsers().then((r) => setUsers(r.users)).catch((e) => notify(e instanceof Error ? e.message : 'Could not load users', true))
  useEffect(() => { reload() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const add = async (e: FormEvent) => {
    e.preventDefault()
    if (password.length < 8) { notify('Password: at least 8 characters', true); return }
    setBusy(true)
    try {
      await api.createUser(email.trim(), password, isAdmin)
      notify(`Added ${email.trim()}`)
      setEmail(''); setPassword(''); setIsAdmin(false)
      reload()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Could not add the user', true)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (u: User) => {
    if (!window.confirm(`Remove ${u.email}? Their decisions, scores and preferences go with them; the postings stay.`)) return
    try {
      await api.deleteUser(u.id)
      notify(`Removed ${u.email}`)
      reload()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Could not remove', true)
    }
  }

  const reset = async (u: User) => {
    if (newPassword.length < 8) { notify('Password: at least 8 characters', true); return }
    try {
      await api.resetUserPassword(u.id, newPassword)
      notify(`New password set for ${u.email}; they are logged out everywhere`)
      setResetting(null); setNewPassword('')
      reload()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Could not reset', true)
    }
  }

  return (
    <div className="profile users">
      <div className="settings-section">Users</div>
      <div className="settings-hint">Everyone who can log in. Only the hash of a password is stored, so a forgotten password can only be replaced, not recovered.</div>
      <ul className="users-list">
        {users.map((u) => (
          <li key={u.id} className="users-row">
            <span className="users-email">{u.email ?? <em>no email</em>}{u.is_admin && <span className="chip chip-admin">admin</span>}{u.id === self.id && <span className="chip">you</span>}</span>
            <span className="users-meta">{u.tokens ?? 0} device{(u.tokens ?? 0) === 1 ? '' : 's'}</span>
            {resetting === u.id ? (
              <span className="users-reset">
                <input type="password" placeholder="new password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
                <button className="btn btn-sm btn-primary" onClick={() => reset(u)}>Set</button>
                <button className="btn btn-sm" onClick={() => { setResetting(null); setNewPassword('') }}>Cancel</button>
              </span>
            ) : (
              <span className="job-actions">
                <button className="btn btn-sm" onClick={() => setResetting(u.id)}>Reset password</button>
                {u.id !== self.id && <button className="btn btn-sm btn-danger" onClick={() => remove(u)}>Remove</button>}
              </span>
            )}
          </li>
        ))}
      </ul>
      <form className="settings-row" onSubmit={add}>
        <input className="grow" type="email" placeholder="email" autoComplete="off" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <input className="grow" type="password" placeholder="password (8+ characters)" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        <label className="settings-check"><input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} /> admin</label>
        <button className="btn btn-primary btn-sm" type="submit" disabled={busy}>Add user</button>
      </form>
    </div>
  )
}
