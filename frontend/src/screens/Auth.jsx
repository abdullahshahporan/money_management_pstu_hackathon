import { useState } from 'react'
import { api } from '../api.js'

const PHONE_RE = /^01[3-9][0-9]{8}$/

export default function Auth({ onAuthenticated }) {
  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({ phone: '', displayName: '', password: '', pin: '' })
  const [status, setStatus] = useState('ready')
  const [error, setError] = useState(null)

  const isRegister = mode === 'register'
  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  const problems = {
    phone: form.phone && !PHONE_RE.test(form.phone) ? 'Use a Bangladeshi number, e.g. 01712345678' : null,
    password:
      form.password && form.password.length < 8 ? 'At least 8 characters' : null,
    pin: isRegister && form.pin && !/^[0-9]{4,6}$/.test(form.pin) ? '4 to 6 digits' : null,
    displayName:
      isRegister && form.displayName && form.displayName.trim().length < 2
        ? 'Please enter your full name'
        : null,
  }

  const ready =
    PHONE_RE.test(form.phone) &&
    form.password.length >= 8 &&
    (!isRegister || (/^[0-9]{4,6}$/.test(form.pin) && form.displayName.trim().length >= 2))

  async function submit(event) {
    event.preventDefault()
    if (!ready || status === 'submitting') return
    setStatus('submitting')
    setError(null)
    try {
      const { data } = isRegister
        ? await api.register({
            phone: form.phone,
            displayName: form.displayName.trim(),
            password: form.password,
            pin: form.pin,
          })
        : await api.login({ phone: form.phone, password: form.password })
      onAuthenticated({
        accessToken: data.accessToken,
        refreshToken: data.refreshToken,
        userId: data.userId,
      })
    } catch (err) {
      setError(err.message)
      setStatus('ready')
    }
  }

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            ৳
          </span>
          <span>Taka</span>
        </div>
      </header>

      <div className="card">
        <h1 className="card__title">{isRegister ? 'Create your account' : 'Welcome back'}</h1>
        <p className="card__hint">
          {isRegister
            ? 'New accounts open with ৳100,000.00 of simulated funds.'
            : 'Sign in to send and request money.'}
        </p>

        {error && (
          <div className="banner banner--error" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={submit} noValidate>
          {isRegister && (
            <div className="field">
              <label htmlFor="name">Full name</label>
              <input
                id="name"
                value={form.displayName}
                onChange={set('displayName')}
                autoComplete="name"
                aria-invalid={Boolean(problems.displayName)}
              />
              {problems.displayName && <p className="field__error">{problems.displayName}</p>}
            </div>
          )}

          <div className="field">
            <label htmlFor="phone">Mobile number</label>
            <input
              id="phone"
              value={form.phone}
              onChange={set('phone')}
              inputMode="numeric"
              placeholder="01712345678"
              autoComplete="tel"
              aria-invalid={Boolean(problems.phone)}
            />
            {problems.phone && <p className="field__error">{problems.phone}</p>}
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={form.password}
              onChange={set('password')}
              autoComplete={isRegister ? 'new-password' : 'current-password'}
              aria-invalid={Boolean(problems.password)}
            />
            {problems.password && <p className="field__error">{problems.password}</p>}
          </div>

          {isRegister && (
            <div className="field">
              <label htmlFor="pin">Transaction PIN</label>
              <input
                id="pin"
                type="password"
                className="pin-input"
                value={form.pin}
                onChange={set('pin')}
                inputMode="numeric"
                maxLength={6}
                aria-describedby="pin-help"
                aria-invalid={Boolean(problems.pin)}
              />
              <p className="card__hint" id="pin-help" style={{ marginTop: 6, marginBottom: 0 }}>
                Separate from your password. Required to authorise every payment.
              </p>
              {problems.pin && <p className="field__error">{problems.pin}</p>}
            </div>
          )}

          <button className="btn-primary" type="submit" disabled={!ready || status === 'submitting'}>
            {status === 'submitting' && <span className="spinner" aria-hidden="true" />}
            {isRegister ? 'Create account' : 'Sign in'}
          </button>
        </form>
      </div>

      <p style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 14 }}>
        {isRegister ? 'Already have an account?' : 'New here?'}{' '}
        <button
          className="btn-link"
          onClick={() => {
            setMode(isRegister ? 'login' : 'register')
            setError(null)
          }}
        >
          {isRegister ? 'Sign in' : 'Create one'}
        </button>
      </p>
    </div>
  )
}
