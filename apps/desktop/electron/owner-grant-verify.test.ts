/**
 * #60 U16: main re-verifies a delivered owner_forward row's stored envelope against the trusted
 * anchor keys (V-12). A forged row (no envelope, a tampered payload, an unknown or revoked kid, a
 * different session or different text) is "unverified".
 */
import { createHash, generateKeyPairSync, sign } from 'node:crypto'
import { describe, expect, test } from 'vitest'

import { verifyStoredOwnerGrant } from './owner-grant-verify'

const PREFIX = Buffer.concat([Buffer.from('hermes-owner-grant/v1', 'ascii'), Buffer.from([0])])

function keypair() {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519')
  const pub = (publicKey.export({ format: 'jwk' }) as { x: string }).x

  return { privateKey, pub, kid: `ok_${pub.slice(0, 8).toLowerCase()}` }
}

function envelopeFor(key: ReturnType<typeof keypair>, overrides: Record<string, unknown> = {}) {
  const text = 'ship it after the review'

  const payload = {
    v: 1,
    issued_at: 1000,
    expires_at: 2000,
    targets: [{ session_id: 'w1', claude_session_id: null }],
    text,
    text_sha256: createHash('sha256').update(text, 'utf8').digest('hex'),
    ...overrides
  }

  const bytes = Buffer.from(JSON.stringify(payload), 'utf8')
  const sig = sign(null, Buffer.concat([PREFIX, bytes]), key.privateKey)

  return {
    text,
    envelope: {
      format: 'hermes-owner-grant/v1',
      kid: key.kid,
      payload: bytes.toString('base64url'),
      sig: sig.toString('base64url')
    }
  }
}

describe('verifyStoredOwnerGrant', () => {
  const key = keypair()
  const anchor = { ok: true as const, keys: [{ kid: key.kid, pub: key.pub, status: 'active', retiredAt: null }] }

  test('a real envelope for this session and text verifies', () => {
    const { envelope, text } = envelopeFor(key)
    expect(verifyStoredOwnerGrant({ envelope, text, sessionId: 'w1' }, anchor)).toEqual({ state: 'verified' })
  })

  test('a row with no envelope (forged metadata) is unverified', () => {
    expect(verifyStoredOwnerGrant({ envelope: null, text: 'x', sessionId: 'w1' }, anchor)).toMatchObject({
      state: 'unverified'
    })
    expect(verifyStoredOwnerGrant({ envelope: { format: 'x' }, text: 'x', sessionId: 'w1' }, anchor)).toMatchObject({
      state: 'unverified'
    })
  })

  test('a tampered payload, other text, or another session is unverified', () => {
    const { envelope, text } = envelopeFor(key)
    const other = envelopeFor(key, { text: 'different' })

    expect(
      verifyStoredOwnerGrant({ envelope: { ...envelope, payload: other.envelope.payload }, text, sessionId: 'w1' }, anchor)
    ).toMatchObject({ state: 'unverified', reason: 'bad_signature' })
    expect(verifyStoredOwnerGrant({ envelope, text: 'something else', sessionId: 'w1' }, anchor)).toMatchObject({
      state: 'unverified',
      reason: 'text_mismatch'
    })
    expect(verifyStoredOwnerGrant({ envelope, text, sessionId: 'w2' }, anchor)).toMatchObject({
      state: 'unverified',
      reason: 'session_mismatch'
    })
  })

  test('an unknown or revoked kid, or no trusted anchor, is unverified', () => {
    const { envelope, text } = envelopeFor(key)
    const stranger = keypair()
    const signedByStranger = envelopeFor(stranger)

    expect(
      verifyStoredOwnerGrant({ envelope: { ...signedByStranger.envelope, kid: key.kid }, text, sessionId: 'w1' }, anchor)
    ).toMatchObject({ state: 'unverified' })
    expect(
      verifyStoredOwnerGrant(
        { envelope, text, sessionId: 'w1' },
        { ok: true, keys: [{ kid: key.kid, pub: key.pub, status: 'revoked', retiredAt: null }] }
      )
    ).toMatchObject({ state: 'unverified', reason: 'key_revoked' })
    expect(verifyStoredOwnerGrant({ envelope, text, sessionId: 'w1' }, { ok: false, keys: [] })).toMatchObject({
      state: 'unverified',
      reason: 'no_anchor'
    })
  })

  test('a retired key still verifies grants issued before it retired', () => {
    const { envelope, text } = envelopeFor(key)
    const retired = { ok: true as const, keys: [{ kid: key.kid, pub: key.pub, status: 'retired', retiredAt: 1500 }] }
    const retiredEarly = { ok: true as const, keys: [{ kid: key.kid, pub: key.pub, status: 'retired', retiredAt: 500 }] }

    expect(verifyStoredOwnerGrant({ envelope, text, sessionId: 'w1' }, retired)).toEqual({ state: 'verified' })
    expect(verifyStoredOwnerGrant({ envelope, text, sessionId: 'w1' }, retiredEarly)).toMatchObject({
      state: 'unverified'
    })
  })
})
