import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { TransactionRow } from './Home.jsx'

export default function History() {
  const [entries, setEntries] = useState([])
  const [cursor, setCursor] = useState(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function load(nextCursor) {
    setLoading(true)
    try {
      const { data } = await api.transactions(nextCursor)
      setEntries((prev) => (nextCursor ? [...prev, ...data.entries] : data.entries))
      setCursor(data.nextCursor)
      setHasMore(data.hasMore)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(null)
  }, [])

  return (
    <div className="card">
      <h2 className="card__title">Statement</h2>
      <p className="card__hint">
        Every movement, newest first, with the balance after each one.
      </p>

      {error && (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      )}

      {entries.length === 0 && !loading ? (
        <p className="empty">No transactions yet.</p>
      ) : (
        entries.map((entry) => <TransactionRow key={entry.entryId} entry={entry} />)
      )}

      {hasMore && (
        <button
          className="btn-secondary"
          style={{ width: '100%', marginTop: 12 }}
          onClick={() => load(cursor)}
          disabled={loading}
        >
          {loading && <span className="spinner" aria-hidden="true" />}
          Load older
        </button>
      )}
    </div>
  )
}
