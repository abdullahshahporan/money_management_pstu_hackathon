import { useEffect, useState } from 'react'
import { api, formatTaka, newIdempotencyKey, parseTakaToMinor } from '../api.js'

const PHONE_RE = /^01[3-9][0-9]{8}$/

export default function Advanced({ account, onDone }) {
  const [mode, setMode] = useState('safepay')
  const [orders, setOrders] = useState([])
  const [spot, setSpot] = useState({ sponsoredPool: null, grants: [], debts: [] })
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const [safeResult, spotResult] = await Promise.all([
        api.safePayList(),
        api.overdraftSummary(),
      ])
      setOrders(safeResult.data.orders)
      setSpot(spotResult.data)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function mutate(work, success) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await work()
      setNotice(success(result.data))
      await load()
      onDone?.()
      return result.data
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="card">
        <h2 className="card__title">Advanced safety features</h2>
        <p className="card__hint">
          SafePay locks commerce funds in escrow. Spot-Me lets trusted people pre-fund a
          tiny shortfall without creating new money.
        </p>
        <div className="btn-row">
          <button
            className={mode === 'safepay' ? 'btn-primary' : 'btn-secondary'}
            onClick={() => setMode('safepay')}
          >
            Conditional SafePay
          </button>
          <button
            className={mode === 'spot' ? 'btn-primary' : 'btn-secondary'}
            onClick={() => setMode('spot')}
          >
            Community Spot-Me
          </button>
        </div>
      </div>

      {error && <div className="banner banner--error">{error}</div>}
      {notice && <div className="banner banner--success">{notice}</div>}

      {mode === 'safepay' ? (
        <SafePayPanel orders={orders} busy={busy} mutate={mutate} />
      ) : (
        <SpotMePanel account={account} spot={spot} busy={busy} mutate={mutate} />
      )}
    </>
  )
}

function SafePayPanel({ orders, busy, mutate }) {
  const [sellerPhone, setSellerPhone] = useState('')
  const [amount, setAmount] = useState('')
  const [description, setDescription] = useState('')
  const [pin, setPin] = useState('')
  const [lastCode, setLastCode] = useState(null)
  const amountMinor = parseTakaToMinor(amount)

  async function create(event) {
    event.preventDefault()
    const data = await mutate(
      () =>
        api.safePayCreate(
          {
            sellerPhone,
            amountMinor,
            pin,
            description: description || null,
          },
          newIdempotencyKey(),
        ),
      (result) => `SafePay ${result.reference} created. Keep the delivery code private.`,
    )
    if (data) {
      setLastCode({ reference: data.reference, code: data.deliveryCode })
      setSellerPhone('')
      setAmount('')
      setDescription('')
      setPin('')
    }
  }

  return (
    <>
      <div className="card">
        <h2 className="card__title">Buy with SafePay</h2>
        <p className="card__hint">
          Your balance is debited now, but the seller receives nothing until delivery is
          verified.
        </p>
        {lastCode && (
          <div className="banner banner--warning">
            <strong>Buyer-only delivery code: {lastCode.code}</strong>
            Give this code to the seller only after receiving {lastCode.reference}.
          </div>
        )}
        <form onSubmit={create}>
          <Field label="Seller mobile">
            <input value={sellerPhone} onChange={(e) => setSellerPhone(e.target.value)} />
          </Field>
          <Field label="Amount (BDT)">
            <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
          </Field>
          <Field label="Product / service">
            <input
              value={description}
              maxLength={200}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <Field label="Transaction PIN">
            <input
              type="password"
              value={pin}
              maxLength={6}
              onChange={(e) => setPin(e.target.value)}
            />
          </Field>
          <button
            className="btn-primary"
            disabled={
              busy || !PHONE_RE.test(sellerPhone) || !amountMinor || pin.length < 4
            }
          >
            Lock funds in SafePay
          </button>
        </form>
      </div>

      <div className="card">
        <h2 className="card__title">Your SafePay orders</h2>
        {orders.length === 0 ? (
          <p className="empty">No SafePay orders yet.</p>
        ) : (
          orders.map((order) => (
            <SafePayOrder key={order.escrowId} order={order} busy={busy} mutate={mutate} />
          ))
        )}
      </div>
    </>
  )
}

function SafePayOrder({ order, busy, mutate }) {
  const [courier, setCourier] = useState('pathao')
  const [tracking, setTracking] = useState('')
  const [code, setCode] = useState('')
  const [reason, setReason] = useState('')
  const [buyerCode, setBuyerCode] = useState(null)
  const open = ['AWAITING_SHIPMENT', 'SHIPPED', 'DELIVERED'].includes(order.status)

  return (
    <div className="txn" style={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <div className="txn__body" style={{ minWidth: 220 }}>
        <div className="txn__title">
          {order.role === 'BUYER' ? 'Buying from' : 'Selling to'} {order.counterpartyName}
        </div>
        <div className="txn__sub">
          {order.reference} · {order.status} · {formatTaka(order.amountMinor)}
        </div>
        {order.description && <div className="txn__sub">{order.description}</div>}
      </div>
      <div style={{ width: '100%', marginTop: 10 }}>
        {order.role === 'BUYER' && open && (
          <>
            <div className="btn-row">
              <button
                className="btn-secondary"
                disabled={busy}
                onClick={async () => {
                  const result = await api.safePayDetail(order.escrowId)
                  setBuyerCode(result.data.deliveryCode)
                }}
              >
                Show delivery code
              </button>
              {['SHIPPED', 'DELIVERED'].includes(order.status) && (
                <button
                  className="btn-primary"
                  disabled={busy}
                  onClick={() =>
                    mutate(
                      () => api.safePayConfirm(order.escrowId, newIdempotencyKey()),
                      () => 'Receipt confirmed; escrow released to the seller.',
                    )
                  }
                >
                  Confirm received
                </button>
              )}
            </div>
            {buyerCode && (
              <div className="banner banner--warning">Buyer-only code: {buyerCode}</div>
            )}
            <div className="field" style={{ marginTop: 10 }}>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Dispute reason (at least 10 characters)"
              />
            </div>
            <button
              className="btn-secondary"
              disabled={busy || reason.trim().length < 10}
              onClick={() =>
                mutate(
                  () => api.safePayDispute(order.escrowId, reason, newIdempotencyKey()),
                  () => 'Escrow frozen and dispute ticket opened.',
                )
              }
            >
              Raise dispute
            </button>
          </>
        )}

        {order.role === 'SELLER' && order.status === 'AWAITING_SHIPMENT' && (
          <div className="btn-row">
            <input value={courier} onChange={(e) => setCourier(e.target.value)} />
            <input
              value={tracking}
              onChange={(e) => setTracking(e.target.value)}
              placeholder="Tracking number"
            />
            <button
              className="btn-primary"
              disabled={busy || tracking.length < 4}
              onClick={() =>
                mutate(
                  () =>
                    api.safePayShip(
                      order.escrowId,
                      { courier: courier.toLowerCase(), trackingNumber: tracking },
                      newIdempotencyKey(),
                    ),
                  () => 'Shipment and tracking number recorded.',
                )
              }
            >
              Mark shipped
            </button>
          </div>
        )}

        {order.role === 'SELLER' && open && (
          <div className="btn-row" style={{ marginTop: 10 }}>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="6-digit buyer code"
              maxLength={6}
            />
            <button
              className="btn-primary"
              disabled={busy || !/^\d{6}$/.test(code)}
              onClick={() =>
                mutate(
                  () => api.safePayReleaseCode(order.escrowId, code, newIdempotencyKey()),
                  () => 'Delivery verified and escrow released.',
                )
              }
            >
              Verify & release
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function SpotMePanel({ account, spot, busy, mutate }) {
  const [poolAmount, setPoolAmount] = useState('')
  const [poolPin, setPoolPin] = useState('')
  const [phone, setPhone] = useState('')
  const [limit, setLimit] = useState('500')
  const [grantPin, setGrantPin] = useState('')
  const poolMinor = parseTakaToMinor(poolAmount)
  const limitMinor = parseTakaToMinor(limit)

  return (
    <>
      <div className="card">
        <h2 className="card__title">
          {spot.sponsoredPool ? 'Top up your Spot-Me pool' : 'Create a Spot-Me pool'}
        </h2>
        <p className="card__hint">
          This moves your real money into a separate non-negative pool. Available account
          balance: {account ? formatTaka(account.balanceMinor) : '—'}.
        </p>
        {spot.sponsoredPool && (
          <div className="banner banner--success">
            Pool balance: {formatTaka(spot.sponsoredPool.balanceMinor)}
          </div>
        )}
        <Field label="Amount (BDT)">
          <input value={poolAmount} onChange={(e) => setPoolAmount(e.target.value)} />
        </Field>
        <Field label="Transaction PIN">
          <input
            type="password"
            value={poolPin}
            maxLength={6}
            onChange={(e) => setPoolPin(e.target.value)}
          />
        </Field>
        <button
          className="btn-primary"
          disabled={busy || !poolMinor || poolPin.length < 4}
          onClick={() =>
            mutate(
              () =>
                spot.sponsoredPool
                  ? api.fundOverdraftPool(
                      { amountMinor: poolMinor, pin: poolPin },
                      newIdempotencyKey(),
                    )
                  : api.createOverdraftPool(
                      { amountMinor: poolMinor, pin: poolPin },
                      newIdempotencyKey(),
                    ),
              () => 'Spot-Me pool funded successfully.',
            )
          }
        >
          {spot.sponsoredPool ? 'Top up pool' : 'Create pool'}
        </button>
      </div>

      {spot.sponsoredPool && (
        <div className="card">
          <h2 className="card__title">Trust a friend or family member</h2>
          <Field label="Beneficiary mobile">
            <input value={phone} onChange={(e) => setPhone(e.target.value)} />
          </Field>
          <Field label="Maximum single draw (BDT, max 500)">
            <input value={limit} onChange={(e) => setLimit(e.target.value)} />
          </Field>
          <Field label="Transaction PIN">
            <input
              type="password"
              value={grantPin}
              maxLength={6}
              onChange={(e) => setGrantPin(e.target.value)}
            />
          </Field>
          <button
            className="btn-primary"
            disabled={
              busy || !PHONE_RE.test(phone) || !limitMinor || limitMinor > 50_000 || grantPin.length < 4
            }
            onClick={() =>
              mutate(
                () =>
                  api.createOverdraftGrant(
                    { beneficiaryPhone: phone, maxDrawMinor: limitMinor, pin: grantPin },
                    newIdempotencyKey(),
                  ),
                () => 'Trusted borrower added to your pool.',
              )
            }
          >
            Grant Spot-Me access
          </button>
          {spot.grants.map((grant) => (
            <div className="txn" key={grant.grantId}>
              <div className="txn__body">
                <div className="txn__title">{grant.beneficiaryName}</div>
                <div className="txn__sub">{grant.beneficiaryPhone}</div>
              </div>
              <div className="txn__amount">up to {formatTaka(grant.maxDrawMinor)}</div>
            </div>
          ))}
        </div>
      )}

      <div className="card">
        <h2 className="card__title">Your Spot-Me debt</h2>
        <p className="card__hint">
          50% of each future incoming payment is intercepted until these zero-interest debts
          are repaid. This happens in the same database transaction as the incoming credit.
        </p>
        {spot.debts.length === 0 ? (
          <p className="empty">No Spot-Me debt.</p>
        ) : (
          spot.debts.map((debt) => (
            <div className="txn" key={debt.loanId}>
              <div className="txn__body">
                <div className="txn__title">Pool from {debt.sponsorName}</div>
                <div className="txn__sub">{debt.status}</div>
              </div>
              <div className="txn__amount">owe {formatTaka(debt.outstandingMinor)}</div>
            </div>
          ))
        )}
      </div>
    </>
  )
}

function Field({ label, children }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
    </div>
  )
}
