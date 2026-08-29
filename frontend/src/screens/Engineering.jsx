import { useCallback, useEffect, useState } from 'react'
import { api, formatTaka, newIdempotencyKey } from '../api.js'

/**
 * The engineering dashboard (spec 26.4).
 *
 * Every number here comes from a live call to the protected reconciliation
 * endpoint, which recomputes each account's balance from the ledger and
 * compares it against the stored balance. Nothing is hardcoded, and nothing
 * is cached - if the books were ever wrong, this screen would say so.
 */
export default function Engineering() {
  const [key, setKey] = useState('demo-engineering-key')
  const [report, setReport] = useState(null)
  const [outbox, setOutbox] = useState(null)
  const [scheduler, setScheduler] = useState(null)
  const [disputes, setDisputes] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [ranAt, setRanAt] = useState(null)

  const run = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [reconcile, outboxStatus, schedulerStatus, disputeQueue] = await Promise.all([
        api.reconcile(key),
        api.outboxStatus(key),
        api.schedulerStatus(key),
        api.safePayDisputes(key),
      ])
      setReport(reconcile.data)
      setOutbox(outboxStatus.data)
      setScheduler(schedulerStatus.data)
      setDisputes(disputeQueue.data.disputes)
      setRanAt(new Date())
    } catch (err) {
      setError(err.message)
      setReport(null)
      setDisputes([])
    } finally {
      setLoading(false)
    }
  }, [key])

  async function resolveDispute(escrowId, decision, note, banBuyer) {
    setResolving(true)
    setError(null)
    try {
      await api.resolveSafePayDispute(
        escrowId,
        { decision, note, banBuyer },
        key,
        newIdempotencyKey(),
      )
      await run()
    } catch (err) {
      setError(err.message)
    } finally {
      setResolving(false)
    }
  }

  useEffect(() => {
    run()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const integrityChecks = report
    ? [
        ['Unbalanced ledger postings', report.unbalanced_ledger_transactions],
        ['Balance mismatches', report.balance_mismatches],
        ['Negative user accounts', report.negative_user_accounts],
        ['Transfers without a ledger', report.succeeded_transfers_without_ledger],
        ['Postings with wrong entry count', report.transfers_with_wrong_entry_count],
        ['Accepted requests without transfer', report.accepted_requests_without_transfer],
        ['Duplicate transfer references', report.duplicate_transfer_references],
        ['System-wide ledger sum', report.system_wide_ledger_sum_minor],
      ]
    : []

  return (
    <>
      <div className="card">
        <h2 className="card__title">Integrity report</h2>
        <p className="card__hint">
          Recomputed from the raw ledger on every run. Every counter must be
          zero — there is no acceptable non-zero value.
        </p>

        <div className="field">
          <label htmlFor="eng-key">Engineering key</label>
          <input id="eng-key" value={key} onChange={(e) => setKey(e.target.value)} />
        </div>

        <button className="btn-primary" onClick={run} disabled={loading}>
          {loading && <span className="spinner" aria-hidden="true" />}
          Run reconciliation
        </button>

        {error && (
          <div className="banner banner--error" role="alert" style={{ marginTop: 14 }}>
            {error}
          </div>
        )}
      </div>

      {report && (
        <>
          <div
            className={`banner ${report.balanced ? 'banner--success' : 'banner--error'}`}
            role="status"
          >
            <strong>
              {report.balanced
                ? 'The books balance'
                : 'DRIFT DETECTED — the books do not balance'}
            </strong>
            {report.accounts_checked} accounts and {report.ledger_entries_checked} ledger
            entries checked
            {ranAt ? ` at ${ranAt.toLocaleTimeString()}` : ''}.
          </div>

          <div className="card">
            <h3 className="card__title">Invariant counters</h3>
            <p className="card__hint">All must read zero.</p>
            <div className="metrics">
              {integrityChecks.map(([label, value]) => (
                <div
                  key={label}
                  className={`metric ${value === 0 ? 'metric--ok' : 'metric--bad'}`}
                >
                  <div className="metric__label">{label}</div>
                  <div className="metric__value">{value}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h3 className="card__title">Closed ecosystem</h3>
            <p className="card__hint">
              The issuance account's negative balance exactly mirrors money held by
              users plus money parked in undo, escrow and community-pool accounts.
            </p>
            <dl style={{ margin: 0 }}>
              <div className="review-row">
                <dt>Money in circulation</dt>
                <dd>{formatTaka(report.money_in_circulation_minor)}</dd>
              </div>
              <div className="review-row">
                <dt>Total user balances</dt>
                <dd>{formatTaka(report.total_user_balance_minor)}</dd>
              </div>
              <div className="review-row">
                <dt>Held / pooled balances</dt>
                <dd>{formatTaka(report.total_held_minor)}</dd>
              </div>
              <div className="review-row">
                <dt>Issuance account</dt>
                <dd>{formatTaka(report.issuance_balance_minor)}</dd>
              </div>
              <div className="review-row review-total">
                <dt>All account types combined</dt>
                <dd>
                  {formatTaka(Object.values(report.balance_by_account_type).reduce((a, b) => a + b, 0))}
                </dd>
              </div>
            </dl>
          </div>
        </>
      )}

      {outbox && (
        <div className="card">
          <h3 className="card__title">Outbox</h3>
          <p className="card__hint">
            Events committed alongside the money they describe. A backlog means
            the broker is unhealthy — it never means a transfer was lost.
          </p>
          <div className="metrics">
            <div className="metric">
              <div className="metric__label">Published</div>
              <div className="metric__value">{outbox.published}</div>
            </div>
            <div className={`metric ${outbox.pending === 0 ? 'metric--ok' : ''}`}>
              <div className="metric__label">Pending</div>
              <div className="metric__value">{outbox.pending}</div>
            </div>
            <div className={`metric ${outbox.deadLettered > 0 ? 'metric--bad' : 'metric--ok'}`}>
              <div className="metric__label">Dead lettered</div>
              <div className="metric__value">{outbox.deadLettered}</div>
            </div>
            <div className="metric">
              <div className="metric__label">Oldest pending</div>
              <div className="metric__value">
                {Math.round(outbox.oldestPendingSeconds)}s
              </div>
            </div>
          </div>
        </div>
      )}

      {scheduler && (
        <div className="card">
          <h3 className="card__title">Deferred settlement worker</h3>
          <p className="card__hint">
            Durable timers for 10-second Undo and SafePay auto-release.
          </p>
          <div className="metrics">
            <div className="metric"><div className="metric__label">Pending</div><div className="metric__value">{scheduler.pending}</div></div>
            <div className={`metric ${scheduler.overdue ? 'metric--bad' : 'metric--ok'}`}><div className="metric__label">Overdue</div><div className="metric__value">{scheduler.overdue}</div></div>
            <div className={`metric ${scheduler.failed ? 'metric--bad' : 'metric--ok'}`}><div className="metric__label">Failed</div><div className="metric__value">{scheduler.failed}</div></div>
            <div className="metric"><div className="metric__label">Completed</div><div className="metric__value">{scheduler.done}</div></div>
          </div>
        </div>
      )}

      {report && (
        <div className="card">
          <h3 className="card__title">SafePay dispute resolution</h3>
          <p className="card__hint">
            Frozen funds stay in ESCROW until one reviewed release or refund decision.
          </p>
          {disputes.length === 0 ? (
            <p className="empty">No unresolved SafePay disputes.</p>
          ) : (
            disputes.map((dispute) => (
              <DisputeCard
                key={dispute.escrowId}
                dispute={dispute}
                disabled={resolving}
                onResolve={resolveDispute}
              />
            ))
          )}
        </div>
      )}
    </>
  )
}

function DisputeCard({ dispute, disabled, onResolve }) {
  const [decision, setDecision] = useState('REFUND')
  const [note, setNote] = useState('')
  const [banBuyer, setBanBuyer] = useState(false)

  return (
    <div className="txn" style={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <div className="txn__body" style={{ minWidth: 260 }}>
        <div className="txn__title">
          {dispute.reference} · {formatTaka(dispute.amountMinor)}
        </div>
        <div className="txn__sub">
          Buyer: {dispute.buyerName} ({dispute.buyerPhone})
        </div>
        <div className="txn__sub">
          Seller: {dispute.sellerName} ({dispute.sellerPhone})
        </div>
        <div className="txn__sub">Buyer claim: {dispute.reason}</div>
        <div className="txn__sub">
          Delivery evidence: {dispute.courier || 'No courier'} ·{' '}
          {dispute.trackingNumber || 'No tracking number'} ·{' '}
          {dispute.deliveredAt ? 'Courier marked delivered' : 'No delivered event'}
        </div>
      </div>
      <div style={{ width: '100%', marginTop: 12 }}>
        <div className="btn-row">
          <select value={decision} onChange={(event) => setDecision(event.target.value)}>
            <option value="REFUND">Refund buyer</option>
            <option value="RELEASE">Release seller</option>
          </select>
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Evidence-based resolution note"
          />
        </div>
        {decision === 'RELEASE' && (
          <label className="check-row">
            <input
              type="checkbox"
              checked={banBuyer}
              onChange={(event) => setBanBuyer(event.target.checked)}
            />
            Close fraudulent buyer account and revoke sessions
          </label>
        )}
        <button
          className="btn-primary"
          disabled={disabled || note.trim().length < 10}
          onClick={() => onResolve(dispute.escrowId, decision, note.trim(), banBuyer)}
        >
          Apply final decision
        </button>
      </div>
    </div>
  )
}
