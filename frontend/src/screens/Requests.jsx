import { useCallback, useEffect, useState } from 'react'
import { api, formatTaka, newIdempotencyKey, parseTakaToMinor } from '../api.js'

const PHONE_RE = /^01[3-9][0-9]{8}$/

export default function Requests({ account, onDone }) {
  const [direction, setDirection] = useState('incoming')
  const [rows, setRows] = useState([])
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ phone: '', amount: '', note: '' })
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [pinFor, setPinFor] = useState(null)
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data } = await api.listRequests(direction)
      setRows(data.requests)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [direction])

  useEffect(() => {
    load()
  }, [load])

  const amountMinor = parseTakaToMinor(form.amount)
  const createReady = PHONE_RE.test(form.phone) && amountMinor !== null && amountMinor > 0

  async function createRequest(event) {
    event.preventDefault()
    if (!createReady || busy) return
    setBusy(true)
    setError(null)
    try {
      const { data } = await api.createRequest({
        payerPhone: form.phone,
        amountMinor,
        note: form.note || null,
      })
      setNotice(`Request ${data.reference} sent.`)
      setForm({ phone: '', amount: '', note: '' })
      setCreating(false)
      setDirection('outgoing')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function accept(request) {
    if (pin.length < 4 || busy) return
    setBusy(true)
    setError(null)
    try {
      const { data } = await api.acceptRequest(request.requestId, pin, newIdempotencyKey())
      setNotice(`Paid. Transfer ${data.transferReference}.`)
      setPinFor(null)
      setPin('')
      await load()
      onDone?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function respond(request, action) {
    setBusy(true)
    setError(null)
    try {
      await (action === 'reject'
        ? api.rejectRequest(request.requestId)
        : api.cancelRequest(request.requestId))
      setNotice(action === 'reject' ? 'Request declined.' : 'Request withdrawn.')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {error && (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="banner banner--success" role="status">
          {notice}
        </div>
      )}

      {creating ? (
        <div className="card">
          <h2 className="card__title">Request money</h2>
          <p className="card__hint">
            They will be asked to approve it. Nothing moves until they do.
          </p>
          <form onSubmit={createRequest} noValidate>
            <div className="field">
              <label htmlFor="payer">Their mobile number</label>
              <input
                id="payer"
                value={form.phone}
                onChange={(e) => setForm({ ...form, phone: e.target.value })}
                inputMode="numeric"
                placeholder="01712345678"
              />
            </div>
            <div className="field">
              <label htmlFor="req-amount">Amount (BDT)</label>
              <input
                id="req-amount"
                className="amount-input"
                value={form.amount}
                onChange={(e) => setForm({ ...form, amount: e.target.value })}
                inputMode="decimal"
                placeholder="0.00"
              />
            </div>
            <div className="field">
              <label htmlFor="req-note">What is it for?</label>
              <input
                id="req-note"
                value={form.note}
                onChange={(e) => setForm({ ...form, note: e.target.value })}
                maxLength={200}
                placeholder="Dinner split"
              />
            </div>
            <div className="btn-row">
              <button type="button" className="btn-secondary" onClick={() => setCreating(false)}>
                Cancel
              </button>
              <button className="btn-primary" type="submit" disabled={!createReady || busy}>
                Send request
              </button>
            </div>
          </form>
        </div>
      ) : (
        <button
          className="btn-primary"
          style={{ marginBottom: 14 }}
          onClick={() => {
            setCreating(true)
            setNotice(null)
          }}
        >
          Request money
        </button>
      )}

      <div className="card">
        <div className="btn-row" style={{ marginBottom: 14 }}>
          <button
            className={direction === 'incoming' ? 'btn-primary' : 'btn-secondary'}
            onClick={() => setDirection('incoming')}
          >
            To pay
          </button>
          <button
            className={direction === 'outgoing' ? 'btn-primary' : 'btn-secondary'}
            onClick={() => setDirection('outgoing')}
          >
            Sent by me
          </button>
        </div>

        {rows.length === 0 ? (
          <p className="empty">
            {direction === 'incoming'
              ? 'Nobody has asked you for money.'
              : 'You have not requested money from anyone.'}
          </p>
        ) : (
          rows.map((request) => (
            <div key={request.requestId} className="txn" style={{ alignItems: 'flex-start' }}>
              <div className="txn__body">
                <div className="txn__title">
                  {formatTaka(request.amountMinor)}{' '}
                  <StatusBadge status={request.status} />
                </div>
                <div className="txn__sub">
                  {request.note ? `${request.note} · ` : ''}
                  {request.reference}
                </div>

                {request.status === 'PENDING' && direction === 'incoming' && (
                  <div style={{ marginTop: 10 }}>
                    {pinFor === request.requestId ? (
                      <>
                        <label htmlFor={`pin-${request.requestId}`}>PIN to authorise</label>
                        <input
                          id={`pin-${request.requestId}`}
                          type="password"
                          className="pin-input"
                          value={pin}
                          onChange={(e) => setPin(e.target.value)}
                          inputMode="numeric"
                          maxLength={6}
                          autoFocus
                        />
                        <div className="btn-row" style={{ marginTop: 8 }}>
                          <button className="btn-secondary" onClick={() => setPinFor(null)}>
                            Cancel
                          </button>
                          <button
                            className="btn-primary"
                            onClick={() => accept(request)}
                            disabled={pin.length < 4 || busy}
                          >
                            Pay
                          </button>
                        </div>
                      </>
                    ) : (
                      <div className="btn-row">
                        <button
                          className="btn-danger"
                          onClick={() => respond(request, 'reject')}
                          disabled={busy}
                        >
                          Decline
                        </button>
                        <button
                          className="btn-primary"
                          onClick={() => {
                            setPinFor(request.requestId)
                            setPin('')
                          }}
                          disabled={busy}
                        >
                          Pay
                        </button>
                      </div>
                    )}
                  </div>
                )}

                {request.status === 'PENDING' && direction === 'outgoing' && (
                  <button
                    className="btn-secondary"
                    style={{ marginTop: 10 }}
                    onClick={() => respond(request, 'cancel')}
                    disabled={busy}
                  >
                    Withdraw request
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </>
  )
}

function StatusBadge({ status }) {
  const tone =
    status === 'ACCEPTED' ? 'badge--ok' : status === 'PENDING' ? 'badge--pending' : 'badge--bad'
  return <span className={`badge ${tone}`}>{status}</span>
}
