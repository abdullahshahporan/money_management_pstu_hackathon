import { useEffect, useState } from 'react'
import { api, formatTaka } from '../api.js'

export default function Home({ account, error, onRefresh, onNavigate }) {
  const [recent, setRecent] = useState([])
  const [pendingCount, setPendingCount] = useState(0)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [txns, requests] = await Promise.all([
          api.transactions(),
          api.listRequests('incoming', 'PENDING'),
        ])
        if (cancelled) return
        setRecent(txns.data.entries.slice(0, 5))
        setPendingCount(requests.data.requests.length)
      } catch {
        /* the balance card already surfaces connectivity problems */
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [account])

  return (
    <>
      {error && (
        <div className="banner banner--error" role="alert">
          <strong>Cannot reach your account</strong>
          {error} — we will not show a stale balance.
        </div>
      )}

      <div className="card balance-card">
        <div className="label">Available balance</div>
        <div className="amount">
          {account ? formatTaka(account.balanceMinor) : '৳—'}
        </div>
        <div className="meta">
          {account ? `${account.currency} · ${account.phone}` : 'Loading…'}
        </div>
      </div>

      <div className="actions">
        <button className="action" onClick={() => onNavigate('send')}>
          <span className="action__icon" aria-hidden="true">
            {'↗'}
          </span>
          <span className="action__label">Send money</span>
          <span className="action__sub">To any Taka user</span>
        </button>
        <button className="action" onClick={() => onNavigate('requests')}>
          <span className="action__icon" aria-hidden="true">
            {'\u{1F4E5}'}
          </span>
          <span className="action__label">Request money</span>
          <span className="action__sub">
            {pendingCount > 0 ? `${pendingCount} awaiting you` : 'Collect what you are owed'}
          </span>
        </button>
      </div>

      <div className="card">
        <h2 className="card__title">Recent activity</h2>
        <p className="card__hint">Your last five movements.</p>
        {recent.length === 0 ? (
          <p className="empty">Nothing yet. Your transfers will appear here.</p>
        ) : (
          recent.map((entry) => <TransactionRow key={entry.entryId} entry={entry} />)
        )}
        <button
          className="btn-secondary"
          style={{ width: '100%', marginTop: 12 }}
          onClick={() => onNavigate('history')}
        >
          View full statement
        </button>
      </div>

      <button className="btn-link" onClick={onRefresh}>
        Refresh balance
      </button>
    </>
  )
}

export function TransactionRow({ entry }) {
  const incoming = entry.direction === 'CREDIT'
  return (
    <div className="txn">
      <span className="txn__icon" aria-hidden="true">
        {incoming ? '↙' : '↗'}
      </span>
      <div className="txn__body">
        <div className="txn__title">
          {entry.kind === 'SIGNUP_GRANT' ? 'Welcome bonus' : entry.counterpartyName}
        </div>
        <div className="txn__sub">
          {/* Direction is stated in words, not only by colour (spec 26.3). */}
          {incoming ? 'Received' : 'Sent'} · {entry.reference}
        </div>
      </div>
      <div className={`txn__amount${incoming ? ' txn__amount--in' : ''}`}>
        {incoming ? '+' : '−'}
        {formatTaka(entry.amountMinor, { withSymbol: false })}
        <span className="txn__balance">
          bal {formatTaka(entry.balanceAfterMinor, { withSymbol: false })}
        </span>
      </div>
    </div>
  )
}
