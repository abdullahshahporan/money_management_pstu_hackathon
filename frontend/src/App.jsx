import { useCallback, useEffect, useState } from 'react'
import { api, setAccessToken, setUnauthorizedHandler } from './api.js'
import Auth from './screens/Auth.jsx'
import Home from './screens/Home.jsx'
import Send from './screens/Send.jsx'
import Requests from './screens/Requests.jsx'
import History from './screens/History.jsx'
import Engineering from './screens/Engineering.jsx'
import Advanced from './screens/Advanced.jsx'

const SESSION_KEY = 'mm.session'

function loadSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function persistSession(session) {
  try {
    if (session) localStorage.setItem(SESSION_KEY, JSON.stringify(session))
    else localStorage.removeItem(SESSION_KEY)
  } catch {
    /* private browsing - the session simply will not survive a reload */
  }
}

// Prime the token before the first render, so a restored session is usable by
// the very first request any screen makes on mount.
const INITIAL_SESSION = loadSession()
setAccessToken(INITIAL_SESSION?.accessToken ?? null)

export default function App() {
  const [session, setSession] = useState(INITIAL_SESSION)
  const [tab, setTab] = useState('home')
  const [account, setAccount] = useState(null)
  const [loadError, setLoadError] = useState(null)

  /**
   * Change the session *and* the auth token in one synchronous step.
   *
   * This must not be a useEffect. React runs effects child-first, so the
   * screen mounted by this state change has its effects run BEFORE the
   * parent's. Setting the token in an effect here meant Home fired its first
   * requests with no Authorization header, got a 401, and the 401 handler
   * signed the user straight back out - login appeared to do nothing at all.
   */
  const applySession = useCallback((next) => {
    setAccessToken(next?.accessToken ?? null)
    persistSession(next)
    setSession(next)
  }, [])

  const signOut = useCallback(() => {
    applySession(null)
    setAccount(null)
    setTab('home')
  }, [applySession])

  useEffect(() => {
    setUnauthorizedHandler(signOut)
  }, [signOut])

  const refreshAccount = useCallback(async () => {
    if (!session) return
    try {
      const { data } = await api.me()
      setAccount(data)
      setLoadError(null)
    } catch (err) {
      setLoadError(err.message)
    }
  }, [session])

  useEffect(() => {
    refreshAccount()
  }, [refreshAccount])

  if (!session) {
    return <Auth onAuthenticated={applySession} />
  }

  const screens = {
    home: <Home account={account} error={loadError} onRefresh={refreshAccount} onNavigate={setTab} />,
    send: <Send account={account} onDone={refreshAccount} onNavigate={setTab} />,
    requests: <Requests account={account} onDone={refreshAccount} />,
    advanced: <Advanced account={account} onDone={refreshAccount} />,
    history: <History />,
    engineering: <Engineering />,
  }

  const tabs = [
    ['home', 'Home', '\u{1F3E0}'],
    ['send', 'Send', '\u{2197}'],
    ['requests', 'Requests', '\u{1F4E5}'],
    ['advanced', 'Safe+', '\u{1F91D}'],
    ['history', 'History', '\u{1F4C4}'],
    ['engineering', 'Trust', '\u{1F6E1}'],
  ]

  return (
    <>
      <div className={tab === 'engineering' ? 'app app--wide' : 'app'}>
        <header className="header">
          <div className="brand">
            <span className="brand__mark" aria-hidden="true">
              ৳
            </span>
            <span>Taka</span>
          </div>
          <button className="btn-link" onClick={signOut}>
            Sign out
          </button>
        </header>
        {screens[tab]}
      </div>

      <nav className="tabbar" aria-label="Primary">
        {tabs.map(([key, label, icon]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            aria-current={tab === key ? 'page' : undefined}
          >
            <span className="tab__icon" aria-hidden="true">
              {icon}
            </span>
            <span>{label}</span>
          </button>
        ))}
      </nav>
    </>
  )
}
