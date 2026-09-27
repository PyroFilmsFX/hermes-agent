/**
 * #60 U16: main re-verifies a delivered owner_forward row's stored grant envelope (V-12), for the
 * renderer's verified / unverified chip. It answers one question: did the owner's anchored key sign
 * THIS text for THIS session? It is display only: gates use `hermes owner verify`, never this.
 *
 * Trust inputs: the root-owned anchor main re-reads per call (`readTrustedOwnerAnchor`) and the
 * envelope bytes. Nothing the renderer sends is believed beyond being the thing to check. Expiry is
 * not checked: a grant that has since expired was still signed by the owner.
 */

import { createHash, createPublicKey, verify as edVerify } from 'node:crypto'

import { GRANT_DOMAIN_PREFIX, GRANT_FORMAT, MAX_PAYLOAD_BYTES } from './owner-grant-key'

export interface VerifyAnchorKey {
  kid: string
  pub: string
  status: string
  retiredAt: number | null
}

export interface VerifyAnchor {
  ok: boolean
  keys: VerifyAnchorKey[]
}

export interface StoredGrantCheck {
  envelope: unknown
  text: unknown
  sessionId: unknown
}

export type StoredGrantVerdict = { state: 'verified' } | { state: 'unverified'; reason: string }

const B64URL_RE = /^[A-Za-z0-9_-]*$/

function unverified(reason: string): StoredGrantVerdict {
  return { state: 'unverified', reason }
}

function b64url(text: unknown): Buffer | null {
  return typeof text === 'string' && B64URL_RE.test(text) ? Buffer.from(text, 'base64url') : null
}

export function verifyStoredOwnerGrant(check: StoredGrantCheck, anchor: VerifyAnchor): StoredGrantVerdict {
  const env = check.envelope as Record<string, unknown> | null

  if (!env || typeof env !== 'object' || env.format !== GRANT_FORMAT || typeof env.kid !== 'string') {
    return unverified('no_envelope')
  }

  if (!anchor.ok) {
    return unverified('no_anchor')
  }

  const key = anchor.keys.find(k => k.kid === env.kid)

  if (!key) {
    return unverified('unknown_key')
  }

  if (key.status === 'revoked') {
    return unverified('key_revoked')
  }

  if (key.status !== 'active' && key.status !== 'retired') {
    return unverified('key_status')
  }

  const payload = b64url(env.payload)
  const sig = b64url(env.sig)

  if (!payload || !sig || payload.length === 0 || payload.length > MAX_PAYLOAD_BYTES || sig.length !== 64) {
    return unverified('malformed')
  }

  let ok = false

  try {
    const publicKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: key.pub }, format: 'jwk' })
    ok = edVerify(null, Buffer.concat([GRANT_DOMAIN_PREFIX, payload]), publicKey, sig)
  } catch {
    ok = false
  }

  if (!ok) {
    return unverified('bad_signature')
  }

  let claims: Record<string, unknown>

  try {
    claims = JSON.parse(payload.toString('utf8'))
  } catch {
    return unverified('malformed')
  }

  if (key.status === 'retired') {
    const issued = claims.issued_at

    if (typeof issued !== 'number' || key.retiredAt === null || issued >= key.retiredAt) {
      return unverified('key_retired')
    }
  }

  if (typeof check.text !== 'string') {
    return unverified('text_mismatch')
  }

  const sha = createHash('sha256').update(check.text, 'utf8').digest('hex')

  if (claims.text_sha256 !== sha) {
    return unverified('text_mismatch')
  }

  const targets = Array.isArray(claims.targets) ? (claims.targets as Array<Record<string, unknown>>) : []

  if (typeof check.sessionId !== 'string' || !targets.some(t => t && t.session_id === check.sessionId)) {
    return unverified('session_mismatch')
  }

  return { state: 'verified' }
}
