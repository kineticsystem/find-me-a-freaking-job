import { useState, type FormEvent } from 'react'
import * as api from '../api'
import type { User } from '../types'

interface Props {
  /** No account exists yet: the form creates the first (admin) one. */
  firstUser: boolean
  onLoggedIn: (user: User) => void
}

export function Login({ firstUser, onLoggedIn }: Props) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [again, setAgain] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (firstUser && password !== again) { setError('The two passwords differ.'); return }
    if (firstUser && password.length < 8) { setError('At least 8 characters.'); return }
    setBusy(true)
    try {
      const r = firstUser ? await api.setupFirstUser(email, password) : await api.login(email, password)
      api.setToken(r.token)
      onLoggedIn(r.user)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not log in')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit} aria-label={firstUser ? 'Create the first account' : 'Log in'}>
        <div className="brand"><div>🎯 Find Me a Freaking Job</div></div>
        {firstUser ? (
          <p className="settings-hint">
            No account exists yet. This first one is the admin and owns everything already in the database — your CV, notes, scores and decisions.
          </p>
        ) : (
          <p className="settings-hint">Log in with the email and password your admin gave you.</p>
        )}
        <label className="login-field">
          <span>Email</span>
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </label>
        <label className="login-field">
          <span>Password</span>
          <input type="password" autoComplete={firstUser ? 'new-password' : 'current-password'} required value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {firstUser && (
          <label className="login-field">
            <span>Password again</span>
            <input type="password" autoComplete="new-password" required value={again} onChange={(e) => setAgain(e.target.value)} />
          </label>
        )}
        {error && <div className="settings-hint error" role="alert">{error}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? '…' : firstUser ? 'Create account' : 'Log in'}
        </button>
      </form>
    </div>
  )
}
