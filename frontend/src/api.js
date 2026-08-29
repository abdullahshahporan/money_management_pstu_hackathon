/**
 * API client.
 *
 * Two behaviours here are load-bearing rather than cosmetic:
 *
 * 1. Every money mutation carries an `Idempotency-Key` that belongs to the
 *    *intent*, not to the HTTP attempt. A retry after a timeout reuses the
 *    same key, so the server returns the original result instead of sending
 *    twice (spec 12.1, 26.3).
 * 2. A network failure or timeout is reported as UNKNOWN, never as failure.
 *    The money may well have moved; claiming otherwise would push the user
 *    into creating a second intent (spec 26.2).
 */

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(code, message, status, retryable, details) {
    super(message)
    this.code = code
    this.status = status
    this.retryable = retryable
    this.details = details
  }
}

/** A request whose outcome the client could not determine. Not a failure. */
export class UnknownOutcomeError extends Error {
  constructor(idempotencyKey) {
    super('We could not confirm whether this went through.')
    this.code = 'UNKNOWN_OUTCOME'
    this.idempotencyKey = idempotencyKey
  }
}

export function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `key-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
}

let accessToken = null
let onUnauthorized = null

export function setAccessToken(token) {
  accessToken = token
}
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

async function request(path, { method = 'GET', body, idempotencyKey, headers = {} } = {}) {
  const finalHeaders = { ...headers }
  if (body !== undefined) finalHeaders['Content-Type'] = 'application/json'
  const sentToken = Boolean(accessToken)
  if (sentToken) finalHeaders['Authorization'] = `Bearer ${accessToken}`
  if (idempotencyKey) finalHeaders['Idempotency-Key'] = idempotencyKey

  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // The request never completed. For a mutation the server may still have
    // committed it, so the honest answer is "unknown".
    if (idempotencyKey) throw new UnknownOutcomeError(idempotencyKey)
    throw new ApiError('NETWORK_ERROR', 'Cannot reach the server.', 0, true)
  }

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    // Only a token we actually sent can be expired or revoked. A 401 on a
    // request that carried no token means the caller was not signed in to
    // begin with, and treating that as "your session ended" would sign the
    // user out of a session that had just started.
    if (response.status === 401 && sentToken && onUnauthorized) onUnauthorized()
    const err = payload?.error ?? {}
    throw new ApiError(
      err.code ?? 'UNKNOWN',
      err.message ?? `Request failed (${response.status}).`,
      response.status,
      err.retryable ?? false,
      err.details,
    )
  }

  return { data: payload?.data, meta: payload?.meta ?? {} }
}

export const api = {
  register: (body) => request('/auth/register', { method: 'POST', body }),
  login: (body) => request('/auth/login', { method: 'POST', body }),
  logout: (refreshToken) =>
    request('/auth/logout', { method: 'POST', body: { refreshToken } }),

  me: () => request('/accounts/me'),
  lookup: (phone) => request(`/accounts/lookup?phone=${encodeURIComponent(phone)}`),
  transactions: (cursor) =>
    request(`/transactions?limit=25${cursor ? `&cursor=${cursor}` : ''}`),

  transfer: (body, idempotencyKey) =>
    request('/transfers', { method: 'POST', body, idempotencyKey }),
  receipt: (reference) => request(`/transfers/${encodeURIComponent(reference)}`),
  pendingUndo: () => request('/transfers/pending-undo'),
  undoTransfer: (id, idempotencyKey) =>
    request(`/transfers/${id}/undo`, { method: 'POST', idempotencyKey }),

  safePayCreate: (body, idempotencyKey) =>
    request('/safepay', { method: 'POST', body, idempotencyKey }),
  safePayList: () => request('/safepay'),
  safePayDetail: (id) => request(`/safepay/${id}`),
  safePayShip: (id, body, idempotencyKey) =>
    request(`/safepay/${id}/ship`, { method: 'POST', body, idempotencyKey }),
  safePayReleaseCode: (id, deliveryCode, idempotencyKey) =>
    request(`/safepay/${id}/release-code`, {
      method: 'POST',
      body: { deliveryCode },
      idempotencyKey,
    }),
  safePayConfirm: (id, idempotencyKey) =>
    request(`/safepay/${id}/confirm-received`, { method: 'POST', idempotencyKey }),
  safePayDispute: (id, reason, idempotencyKey) =>
    request(`/safepay/${id}/dispute`, {
      method: 'POST',
      body: { reason },
      idempotencyKey,
    }),

  overdraftSummary: () => request('/overdraft'),
  createOverdraftPool: (body, idempotencyKey) =>
    request('/overdraft/pools', { method: 'POST', body, idempotencyKey }),
  fundOverdraftPool: (body, idempotencyKey) =>
    request('/overdraft/pools/fund', { method: 'POST', body, idempotencyKey }),
  createOverdraftGrant: (body, idempotencyKey) =>
    request('/overdraft/grants', { method: 'POST', body, idempotencyKey }),

  createRequest: (body) => request('/money-requests', { method: 'POST', body }),
  listRequests: (direction, status) =>
    request(`/money-requests?direction=${direction}${status ? `&status=${status}` : ''}`),
  acceptRequest: (id, pin, idempotencyKey) =>
    request(`/money-requests/${id}/accept`, {
      method: 'POST',
      body: { pin },
      idempotencyKey,
    }),
  rejectRequest: (id) => request(`/money-requests/${id}/reject`, { method: 'POST' }),
  cancelRequest: (id) => request(`/money-requests/${id}/cancel`, { method: 'POST' }),

  reconcile: (engineeringKey) =>
    request('/engineering/reconcile', { headers: { 'X-Engineering-Key': engineeringKey } }),
  outboxStatus: (engineeringKey) =>
    request('/engineering/outbox', { headers: { 'X-Engineering-Key': engineeringKey } }),
  schedulerStatus: (engineeringKey) =>
    request('/engineering/scheduler', { headers: { 'X-Engineering-Key': engineeringKey } }),
  safePayDisputes: (engineeringKey) =>
    request('/engineering/safepay/disputes', {
      headers: { 'X-Engineering-Key': engineeringKey },
    }),
  resolveSafePayDispute: (id, body, engineeringKey, idempotencyKey) =>
    request(`/engineering/safepay/${id}/resolve`, {
      method: 'POST',
      body,
      idempotencyKey,
      headers: { 'X-Engineering-Key': engineeringKey },
    }),
  ready: () => request('/health/ready'),
}

/** Format integer minor units as BDT. Never parse money as a float. */
export function formatTaka(minor, { withSymbol = true } = {}) {
  const negative = minor < 0
  const abs = Math.abs(minor)
  const major = Math.trunc(abs / 100)
  const fraction = String(abs % 100).padStart(2, '0')
  const grouped = major.toLocaleString('en-US')
  return `${negative ? '-' : ''}${withSymbol ? '৳' : ''}${grouped}.${fraction}`
}

/** Parse a typed amount into integer minor units, textually. */
export function parseTakaToMinor(input) {
  const trimmed = String(input).trim()
  if (!/^[0-9]{1,9}(\.[0-9]{1,2})?$/.test(trimmed)) return null
  const [major, fraction = ''] = trimmed.split('.')
  return Number(major) * 100 + Number(fraction.padEnd(2, '0'))
}
