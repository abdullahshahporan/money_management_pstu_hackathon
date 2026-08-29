import { useEffect, useState } from 'react'
import {
  UnknownOutcomeError,
  api,
  formatTaka,
  newIdempotencyKey,
  parseTakaToMinor,
} from '../api.js'

const PHONE_RE = /^01[3-9][0-9]{8}$/

/**
 * The five-state transaction UX from spec 26.2:
 *
 *   compose -> review -> submitting -> [succeeded | rejected | unknown]
 *
 * `unknown` is the state most apps get wrong. When a request times out the
 * money may well have moved, so we must not say "failed" and we must not
 * silently start a second payment. We keep the same idempotency key, offer to
 * check again, and let the server decide - the retry either replays the
 * original result or completes the original intent.
 */
export default function Send({ account, onDone, onNavigate }) {
  const [step, setStep] = useState('compose')
  const [phone, setPhone] = useState('')
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [pin, setPin] = useState('')
  const [recipient, setRecipient] = useState(null)
  const [receipt, setReceipt] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  // One key per *intent*. Created when the user commits to the review screen
  // and deliberately kept across retries.
  const [idempotencyKey, setIdempotencyKey] = useState(null)
  const [undoKey, setUndoKey] = useState(null)
  const [secondsLeft, setSecondsLeft] = useState(0)

  const amountMinor = parseTakaToMinor(amount)
  const composeReady = PHONE_RE.test(phone) && amountMinor !== null && amountMinor > 0
  const insufficient = account && amountMinor !== null && amountMinor > account.balanceMinor

  useEffect(() => {
    if (!receipt?.undoExpiresAt || receipt.status !== 'PENDING_UNDO') return undefined
    const update = () => {
      const left = Math.max(0, Math.ceil((Date.parse(receipt.undoExpiresAt) - Date.now()) / 1000))
      setSecondsLeft(left)
    }
    update()
    const timer = setInterval(update, 250)
    return () => clearInterval(timer)
  }, [receipt])

  function reset() {
    setStep('compose')
    setPhone('')
    setAmount('')
    setNote('')
    setPin('')
    setRecipient(null)
    setReceipt(null)
    setError(null)
    setIdempotencyKey(null)
    setUndoKey(null)
  }

  async function lookupRecipient(event) {
    event.preventDefault()
    if (!composeReady || busy) return
    setBusy(true)
    setError(null)
    try {
      const { data } = await api.lookup(phone)
      setRecipient(data)
      setIdempotencyKey(newIdempotencyKey())
      setStep('review')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function confirmSend(event) {
    event?.preventDefault()
    if (busy || pin.length < 4) return
    setBusy(true)
    setStep('submitting')
    setError(null)
    try {
      const { data, meta } = await api.transfer(
        { recipientPhone: phone, amountMinor, pin, note: note || null },
        idempotencyKey,
      )
      setReceipt({ ...data, replayed: meta.idempotentReplay })
      setUndoKey(newIdempotencyKey())
      setStep('succeeded')
      onDone?.()
    } catch (err) {
      if (err instanceof UnknownOutcomeError) {
        // Do NOT report failure. The transfer may have committed.
        setStep('unknown')
      } else {
        setError(err.message)
        setStep('review')
      }
    } finally {
      setBusy(false)
    }
  }

  /* ---------------- compose ---------------- */
  if (step === 'compose') {
    return (
      <div className="card">
        <h2 className="card__title">Send money</h2>
        <p className="card__hint">
          Available: {account ? formatTaka(account.balanceMinor) : '—'}
        </p>

        {error && (
          <div className="banner banner--error" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={lookupRecipient} noValidate>
          <div className="field">
            <label htmlFor="to">Recipient mobile number</label>
            <input
              id="to"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              inputMode="numeric"
              placeholder="01712345678"
              autoComplete="off"
            />
            {phone && !PHONE_RE.test(phone) && (
              <p className="field__error">Use a Bangladeshi number, e.g. 01712345678</p>
            )}
          </div>

          <div className="field">
            <label htmlFor="amount">Amount (BDT)</label>
            <input
              id="amount"
              className="amount-input"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              inputMode="decimal"
              placeholder="0.00"
            />
            {amount && amountMinor === null && (
              <p className="field__error">Enter an amount like 2500 or 2500.50</p>
            )}
            {insufficient && (
              <p className="card__hint">
                You are {formatTaka(amountMinor - account.balanceMinor)} short. If a trusted
                Spot-Me pool covers it, the server will borrow exactly that amount; otherwise
                the transfer is safely rejected.
              </p>
            )}
          </div>

          <div className="field">
            <label htmlFor="note">Note (optional)</label>
            <input
              id="note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={200}
              placeholder="Lunch"
            />
          </div>

          <button className="btn-primary" type="submit" disabled={!composeReady || busy}>
            {busy && <span className="spinner" aria-hidden="true" />}
            Continue
          </button>
        </form>
      </div>
    )
  }

  /* ---------------- review ---------------- */
  if (step === 'review') {
    return (
      <div className="card">
        <h2 className="card__title">Confirm transfer</h2>
        <p className="card__hint">Check the recipient carefully before you pay.</p>

        {error && (
          <div className="banner banner--error" role="alert">
            {error}
          </div>
        )}

        <dl style={{ margin: '0 0 16px' }}>
          <div className="review-row">
            <dt>To</dt>
            <dd>
              {recipient.displayName}
              <br />
              <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>
                {recipient.phone}
              </span>
            </dd>
          </div>
          <div className="review-row">
            <dt>Amount</dt>
            <dd>{formatTaka(amountMinor)}</dd>
          </div>
          <div className="review-row">
            <dt>Fee</dt>
            <dd>{formatTaka(0)}</dd>
          </div>
          {note && (
            <div className="review-row">
              <dt>Note</dt>
              <dd style={{ fontWeight: 400 }}>{note}</dd>
            </div>
          )}
          <div className="review-row review-total">
            <dt>Total</dt>
            <dd>{formatTaka(amountMinor)}</dd>
          </div>
        </dl>

        <form onSubmit={confirmSend}>
          <div className="field">
            <label htmlFor="pin">Enter your PIN to authorise</label>
            <input
              id="pin"
              type="password"
              className="pin-input"
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              inputMode="numeric"
              maxLength={6}
              autoFocus
            />
          </div>

          <div className="btn-row">
            <button type="button" className="btn-secondary" onClick={() => setStep('compose')}>
              Back
            </button>
            <button className="btn-primary" type="submit" disabled={pin.length < 4 || busy}>
              Pay {formatTaka(amountMinor)}
            </button>
          </div>
        </form>
      </div>
    )
  }

  /* ---------------- submitting ---------------- */
  if (step === 'submitting') {
    return (
      <div className="card">
        <h2 className="card__title">
          <span className="spinner" aria-hidden="true" />
          Sending…
        </h2>
        <p className="card__hint" role="status">
          Please wait. Do not close this screen — we will confirm once the
          transfer is committed.
        </p>
      </div>
    )
  }

  /* ---------------- unknown outcome ---------------- */
  if (step === 'unknown') {
    return (
      <div className="card">
        <h2 className="card__title">Checking transaction status</h2>
        <div className="banner banner--warning" role="alert">
          <strong>We could not confirm this transfer</strong>
          The connection dropped before we heard back. Your money may already
          have been sent, so we will not simply try again.
        </div>
        <p className="card__hint">
          Checking again uses the same request identifier, so it can never send
          twice: the server either returns the original receipt or completes
          the transfer you already started.
        </p>
        <div className="btn-row">
          <button className="btn-secondary" onClick={() => onNavigate('history')}>
            View history
          </button>
          <button className="btn-primary" onClick={() => confirmSend()} disabled={busy}>
            {busy && <span className="spinner" aria-hidden="true" />}
            Check again
          </button>
        </div>
      </div>
    )
  }

  /* ---------------- receipt ---------------- */
  return (
    <div className="card">
      <div className="banner banner--success" role="status">
        <strong>
          {receipt.status === 'PENDING_UNDO' ? 'Money locked — undo is available' : 'Money sent'}
        </strong>
        {formatTaka(receipt.amountMinor)} to {recipient.displayName}
        {receipt.replayed && ' (this request had already been processed)'}
        {receipt.overdraftUsed && (
          <> · Spot-Me covered {formatTaka(receipt.overdraft.amountMinor)}</>
        )}
      </div>

      <dl style={{ margin: '0 0 16px' }}>
        <div className="review-row">
          <dt>Reference</dt>
          <dd style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>{receipt.reference}</dd>
        </div>
        <div className="review-row">
          <dt>Status</dt>
          <dd>
            <span className="badge badge--ok">
              {receipt.status === 'PENDING_UNDO' ? `PENDING · ${secondsLeft}s` : receipt.status}
            </span>
          </dd>
        </div>
        <div className="review-row">
          <dt>New balance</dt>
          <dd>{formatTaka(receipt.senderBalanceMinor)}</dd>
        </div>
      </dl>

      {receipt.status === 'PENDING_UNDO' && secondsLeft > 0 && (
        <button
          className="btn-primary"
          style={{ width: '100%', marginBottom: 12 }}
          disabled={busy}
          onClick={async () => {
            setBusy(true)
            setError(null)
            try {
              const { data } = await api.undoTransfer(receipt.transferId, undoKey)
              setReceipt((current) => ({ ...current, ...data, status: 'REFUNDED' }))
              onDone?.()
            } catch (err) {
              setError(err.message)
            } finally {
              setBusy(false)
            }
          }}
        >
          Undo transfer ({secondsLeft}s)
        </button>
      )}
      {receipt.status === 'REFUNDED' && (
        <div className="banner banner--warning">Transfer undone and money returned.</div>
      )}
      {error && <div className="banner banner--error">{error}</div>}

      <div className="btn-row">
        <button className="btn-secondary" onClick={() => onNavigate('home')}>
          Done
        </button>
        <button className="btn-primary" onClick={reset}>
          Send again
        </button>
      </div>
    </div>
  )
}
