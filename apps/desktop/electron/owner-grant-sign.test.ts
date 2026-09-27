/**
 * #60 U12 signing core: the v1 payload, the confirm model the native dialog renders, Touch ID
 * for prod, one grant per backend under one confirm, and the grant file (VERIFY addendum §2,
 * §6, E-7, E-8). The dialog itself is NOT here: `confirm` and Touch ID are injected ports.
 *
 * safeStorage is a mock; grants dirs are mkdtemp dirs under os.tmpdir(), never ~/.hermes.
 */

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, test, vi } from 'vitest'

import { createOwnerKeyStore, kidForPub, type SafeStorageLike } from './owner-grant-key'
import {
  confirmAndSignGrants,
  type ConfirmModel,
  deriveGrantId,
  encodePayload,
  GRANT_AUDIENCE,
  grantFileName,
  parseScope,
  SCOPE_CLASS_POLICY,
  SCOPE_LABELS,
  type SignedOutcome,
  type SignPorts,
  type SignRequest,
  writeGrantFile
} from './owner-grant-sign'

const tmpDirs: string[] = []

function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ogs-'))
  tmpDirs.push(dir)

  return dir
}

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

const mockSafeStorage: SafeStorageLike = {
  isEncryptionAvailable: () => true,
  encryptString: (plain: string) => Buffer.from('W:' + Buffer.from(plain).toString('base64')),
  decryptString: (wrapped: Buffer) => Buffer.from(wrapped.toString().slice(2), 'base64').toString()
}

const NOW = 1_790_000_000_000
const UID = 501

function readyStore(opts: { anchored?: boolean } = {}) {
  const base = tmpDir()
  const store = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
  const info = store.ensure()

  if (opts.anchored !== false) {
    store.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })
  }

  return { store, info, grantsDir: path.join(base, 'grants') }
}

function request(overrides: Partial<SignRequest> = {}): SignRequest {
  return {
    text: 'Enable the review budget gate for this lane.',
    gesture: 'proposal',
    sourceSession: { session_id: 'mgr-1', message_id: 'm-9', role: 'user' },
    targets: [{ profile: 'default', session_id: 'w-1', claude_session_id: 'c-1', backend: 'bk_a' }],
    scope: ['conductor:gate:review-budget-enable'],
    ...overrides
  }
}

function ports(
  base: { store: SignPorts['store']; grantsDir: string },
  over: Partial<SignPorts> = {}
): SignPorts & { confirm: ReturnType<typeof vi.fn> } {
  const confirm = vi.fn(async (_model: ConfirmModel) => ({ confirmed: true, acknowledgedUnrecognized: false }))

  return {
    store: base.store,
    grantsDir: base.grantsDir,
    now: () => NOW,
    ownerUid: UID,
    confirm,
    touchId: { canPrompt: () => false, prompt: vi.fn(async () => {}) },
    ...over
  } as SignPorts & { confirm: ReturnType<typeof vi.fn> }
}

function listGrants(dir: string): string[] {
  return fs.existsSync(dir) ? fs.readdirSync(dir).filter(n => n.endsWith('.json')) : []
}

function payloadOf(envelope: { payload: string }) {
  return JSON.parse(Buffer.from(envelope.payload, 'base64url').toString('utf8'))
}

describe('scope catalog parity with hermes_owner_grant/scopes.json (U9 shared by Python and TS)', () => {
  test('class policy and labels are identical to the Python catalog', () => {
    const catalogPath = path.resolve(__dirname, '../../../hermes_owner_grant/scopes.json')
    const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'))
    expect(SCOPE_CLASS_POLICY).toEqual(catalog.classes)
    expect(SCOPE_LABELS).toEqual(Object.fromEntries(catalog.scopes.map((e: any) => [e.scope, e.label])))
  })

  test('grammar: exact names, no wildcards, unknown in-grammar names are flagged uncatalogued', () => {
    expect(parseScope('conductor:prod:target')).toMatchObject({ scopeClass: 'prod', catalogued: true })
    expect(parseScope('conductor:gate:new-thing')).toMatchObject({ scopeClass: 'gate', catalogued: false })

    for (const bad of ['conductor:gate:*', 'conductor:root:x', 'conductor:gate:', 'Conductor:gate:x', 'conductor:gate:-x']) {
      expect(() => parseScope(bad)).toThrow()
    }
  })
})

describe('E-7: the confirm model lists scopes, classes and expiry; prod needs Touch ID; Cancel writes nothing', () => {
  test('E-7a every target and every scope is its own line with class, label, single-use and expiry', async () => {
    const base = readyStore()
    const p = ports(base)

    const req = request({
      scope: ['conductor:marker:restore', 'conductor:gate:review-budget-enable'],
      targets: [
        { profile: 'default', session_id: 'w-2', claude_session_id: 'c-2', backend: 'bk_a', title: 'Worker two' },
        { profile: 'default', session_id: 'w-1', claude_session_id: 'c-1', backend: 'bk_a', title: 'Worker one' }
      ]
    })

    await confirmAndSignGrants(req, p)

    const model: ConfirmModel = p.confirm.mock.calls[0][0]
    expect(model.targets.map(t => t.session_id)).toEqual(['w-1', 'w-2'])
    expect(model.targets[0]).toMatchObject({ profile: 'default', title: 'Worker one' })
    expect(model.scopes).toEqual([
      expect.objectContaining({ scope: 'conductor:gate:review-budget-enable', scopeClass: 'gate', label: 'Enable review budget', singleUse: false, catalogued: true }),
      expect.objectContaining({ scope: 'conductor:marker:restore', scopeClass: 'marker', label: 'Restore marker', singleUse: true, catalogued: true })
    ])
    // Mixed classes: the grant lives as long as the tightest class default (marker, 1 h).
    expect(model.ttlMs).toBe(3_600_000)
    expect(model.expiresAt).toBe(NOW + 3_600_000)
    expect(model.text).toBe(req.text)
    expect(model.textChars).toBe(req.text.length)
    expect(model.quoteOnly).toBe(false)
    expect(model.requiresTouchId).toBe(false)
  })

  test('E-7b prod: a subject is required before any dialog, and Touch ID gates signing when available', async () => {
    const base = readyStore()
    const noSubject = ports(base)
    await expect(confirmAndSignGrants(request({ scope: ['conductor:prod:target'] }), noSubject)).rejects.toMatchObject({
      code: 'subject_required'
    })
    expect(noSubject.confirm).not.toHaveBeenCalled()

    const touch = { canPrompt: () => true, prompt: vi.fn(async () => {}) }
    const p = ports(base, { touchId: touch })
    const req = request({ scope: ['conductor:prod:target'], subject: { 'conductor:prod:target': 'sha256:abc' } })
    const out = await confirmAndSignGrants(req, p)
    const model: ConfirmModel = p.confirm.mock.calls[0][0]
    expect(model.requiresTouchId).toBe(true)
    expect(model.scopes[0]).toMatchObject({ scopeClass: 'prod', subject: 'sha256:abc', singleUse: true })
    expect(model.ttlMs).toBe(900_000)
    expect(touch.prompt).toHaveBeenCalledTimes(1)
    expect(out.cancelled).toBe(false)
    const payload = payloadOf((out as SignedOutcome).grants[0].envelope)
    expect(payload.confirm).toBe('touch_id')
    expect(payload.subject).toEqual({ 'conductor:prod:target': 'sha256:abc' })
    expect(payload.single_use).toEqual(['conductor:prod:target'])
  })

  test('E-7c a failed or cancelled Touch ID writes nothing and signs nothing', async () => {
    const base = readyStore()
    const sign = vi.spyOn(base.store, 'signEnvelope')
    const touch = { canPrompt: () => true, prompt: vi.fn(async () => Promise.reject(new Error('cancelled'))) }
    const req = request({ scope: ['conductor:prod:target'], subject: { 'conductor:prod:target': 'deploy-7' } })

    await expect(confirmAndSignGrants(req, ports(base, { touchId: touch }))).resolves.toEqual({ cancelled: true, reason: 'touch_id' })
    expect(sign).not.toHaveBeenCalled()
    expect(listGrants(base.grantsDir)).toEqual([])
  })

  test('E-7d Cancel in the dialog writes nothing and signs nothing', async () => {
    const base = readyStore()
    const sign = vi.spyOn(base.store, 'signEnvelope')
    const p = ports(base, { confirm: vi.fn(async () => ({ confirmed: false })) as any })

    await expect(confirmAndSignGrants(request(), p)).resolves.toEqual({ cancelled: true, reason: 'dialog' })
    expect(sign).not.toHaveBeenCalled()
    expect(listGrants(base.grantsDir)).toEqual([])
  })

  test('E-7e an out-of-grammar scope is refused before the dialog; an unrecognized one needs the extra acknowledgement', async () => {
    const base = readyStore()
    const p = ports(base)
    await expect(confirmAndSignGrants(request({ scope: ['conductor:gate:*'] }), p)).rejects.toMatchObject({ code: 'bad_scope' })
    expect(p.confirm).not.toHaveBeenCalled()

    const unknown = request({ scope: ['conductor:gate:brand-new-gate'] })
    const noAck = ports(base)
    await expect(confirmAndSignGrants(unknown, noAck)).resolves.toEqual({ cancelled: true, reason: 'unrecognized_scope' })
    expect(noAck.confirm.mock.calls[0][0].requiresUnrecognizedAck).toBe(true)
    expect(noAck.confirm.mock.calls[0][0].scopes[0]).toMatchObject({ catalogued: false, label: null })
    expect(listGrants(base.grantsDir)).toEqual([])

    const ack = ports(base, { confirm: vi.fn(async () => ({ confirmed: true, acknowledgedUnrecognized: true })) as any })
    expect((await confirmAndSignGrants(unknown, ack)).cancelled).toBe(false)
  })

  test('E-7f any grant, quote-only included, is refused before the dialog when the anchor does not pin this key', async () => {
    const base = readyStore({ anchored: false })
    const p = ports(base)
    await expect(confirmAndSignGrants(request(), p)).rejects.toMatchObject({ code: 'anchor_missing' })
    // A quote-only forward too: backends only trust the root-owned anchor, never an env key.
    await expect(confirmAndSignGrants(request({ scope: [] }), p)).rejects.toMatchObject({ code: 'anchor_missing' })
    base.store.setAnchor({ keys: [{ kid: 'ok_0000000000000000', pub: base.info.pub, status: 'active' }] })
    await expect(confirmAndSignGrants(request({ scope: [] }), p)).rejects.toMatchObject({ code: 'anchor_mismatch' })
    expect(p.confirm).not.toHaveBeenCalled()
    expect(listGrants(base.grantsDir)).toEqual([])
  })

  test('E-7g self-targets only for composer_signed, and composer_signed only targets its own session', async () => {
    const base = readyStore()
    const self = [{ profile: 'default', session_id: 'mgr-1', claude_session_id: 'c-mgr', backend: 'bk_a' }]
    await expect(confirmAndSignGrants(request({ targets: self }), ports(base))).rejects.toMatchObject({ code: 'self_target' })
    await expect(
      confirmAndSignGrants(request({ gesture: 'composer_signed' }), ports(base))
    ).rejects.toMatchObject({ code: 'self_target' })
    const out = await confirmAndSignGrants(request({ gesture: 'composer_signed', targets: self }), ports(base))
    expect(out.cancelled).toBe(false)
  })

  test('E-7h refuses >5 targets, empty text, a TTL above the class max is clamped to it', async () => {
    const base = readyStore()
    const six = Array.from({ length: 6 }, (_, i) => ({ profile: 'default', session_id: `w-${i}`, claude_session_id: null, backend: 'bk_a' }))
    await expect(confirmAndSignGrants(request({ targets: six }), ports(base))).rejects.toMatchObject({ code: 'too_many_targets' })
    await expect(confirmAndSignGrants(request({ text: '   ' }), ports(base))).rejects.toMatchObject({ code: 'empty_text' })
    const p = ports(base)
    await confirmAndSignGrants(request({ ttlMs: 10 * 24 * 3_600_000 }), p)
    expect(p.confirm.mock.calls[0][0].ttlMs).toBe(259_200_000)
  })
})

describe('E-8: one confirm makes one grant per backend, sharing decision_id, written 0600 with O_EXCL', () => {
  test('E-8a three targets on two backends give two grants with one decision_id and distinct nonces', async () => {
    const base = readyStore()
    const p = ports(base)

    const req = request({
      text: 'Ship it 🚀 once CI is green.',
      targets: [
        { profile: 'default', session_id: 'w-3', claude_session_id: 'c-3', backend: 'bk_b' },
        { profile: 'work', session_id: 'w-2', claude_session_id: 'c-2', backend: 'bk_a' },
        { profile: 'default', session_id: 'w-1', claude_session_id: 'c-1', backend: 'bk_a' }
      ]
    })

    const out = await confirmAndSignGrants(req, p)
    expect(p.confirm).toHaveBeenCalledTimes(1)

    if (out.cancelled) {
      throw new Error('expected grants')
    }

    const signed = out as SignedOutcome

    expect(signed.grants.map(g => g.backend)).toEqual(['bk_a', 'bk_b'])
    const [a, b] = signed.grants.map(g => payloadOf(g.envelope))
    expect(a.decision_id).toMatch(/^od_[a-z2-7]{26}$/)
    expect(b.decision_id).toBe(a.decision_id)
    expect(signed.decisionId).toBe(a.decision_id)
    expect(a.nonce).not.toBe(b.nonce)
    expect(a.nonce).toMatch(/^[A-Za-z0-9_-]{22}$/)
    expect(a.targets).toEqual([
      { session_id: 'w-1', claude_session_id: 'c-1' },
      { session_id: 'w-2', claude_session_id: 'c-2' }
    ])
    expect(a.forward_targets).toEqual(['default:w-1', 'work:w-2'])
    expect(b.targets).toEqual([{ session_id: 'w-3', claude_session_id: 'c-3' }])
    expect(b.forward_targets).toEqual(['default:w-3'])
    expect(a.backend).toBe('bk_a')
    expect(b.backend).toBe('bk_b')

    // The v1 payload the verifier requires (addendum §2.2).
    expect(a).toMatchObject({
      v: 1,
      aud: GRANT_AUDIENCE,
      issued_at: NOW,
      deliver_by: NOW + 60_000,
      expires_at: NOW + 43_200_000,
      owner_uid: UID,
      gesture: 'proposal',
      confirm: 'native_dialog',
      source_session: { session_id: 'mgr-1', message_id: 'm-9', role: 'user' },
      scope: ['conductor:gate:review-budget-enable'],
      single_use: [],
      subject: {},
      text: req.text,
      text_sha256: createHash('sha256').update(req.text, 'utf8').digest('hex'),
      text_len: Buffer.byteLength(req.text, 'utf8')
    })
    expect(a.text_len).not.toBe(req.text.length) // UTF-8 bytes, not UTF-16 units

    // Files: one per grant, named <issued_at>-<grant_id>.json, 0600, holding the envelope.
    const names = listGrants(base.grantsDir).sort()
    expect(names).toEqual(signed.grants.map(g => `${NOW}-${g.grantId}.json`).sort())
    expect(fs.statSync(base.grantsDir).mode & 0o777).toBe(0o700)

    for (const g of signed.grants) {
      expect(g.grantId).toBe(deriveGrantId(Buffer.from(g.envelope.payload, 'base64url')))
      expect(g.envelope.kid).toBe(base.info.kid)
      expect(fs.statSync(g.path).mode & 0o777).toBe(0o600)
      expect(JSON.parse(fs.readFileSync(g.path, 'utf8'))).toEqual(g.envelope)
    }

    // No temp files are left behind.
    expect(fs.readdirSync(base.grantsDir).filter(n => !n.endsWith('.json'))).toEqual([])
  })

  test('E-8b writeGrantFile never replaces an existing grant (O_EXCL / no clobber)', () => {
    const dir = path.join(tmpDir(), 'grants')
    const env = { format: 'hermes-owner-grant/v1', kid: 'ok_0123456789abcdef', payload: 'e30', sig: 'AA' }
    const first = writeGrantFile(dir, env, NOW, 'og_' + 'a'.repeat(26))
    expect(fs.statSync(first).mode & 0o777).toBe(0o600)
    const before = fs.readFileSync(first)
    expect(() => writeGrantFile(dir, { ...env, sig: 'BB' }, NOW, 'og_' + 'a'.repeat(26))).toThrowError(
      expect.objectContaining({ code: 'grant_exists' })
    )
    expect(fs.readFileSync(first).equals(before)).toBe(true)
    expect(fs.readdirSync(dir)).toEqual([path.basename(first)])
  })

  test('E-8c the grant file is written before the result is returned, and a write failure returns no envelope', async () => {
    const base = readyStore()
    const blocked = path.join(tmpDir(), 'not-a-dir')
    fs.writeFileSync(blocked, 'x')
    await expect(confirmAndSignGrants(request(), ports(base, { grantsDir: path.join(blocked, 'grants') }))).rejects.toThrow()
  })

  test('E-8d payload encoding is compact JSON with recursively sorted keys; grant ids are og_ + base32(sha256)[:26]', () => {
    const bytes = encodePayload({ b: 1, a: { d: [3, { z: 1, y: 2 }], c: 'é' } })
    expect(bytes.toString('utf8')).toBe('{"a":{"c":"é","d":[3,{"y":2,"z":1}]},"b":1}')
    expect(() => encodePayload({ x: 1.5 })).toThrow()
    expect(() => encodePayload({ x: '\ud800' })).toThrow()
    expect(deriveGrantId(Buffer.from('abc'))).toMatch(/^og_[a-z2-7]{26}$/)
    expect(grantFileName(NOW, 'og_' + 'b'.repeat(26))).toBe(`${NOW}-og_${'b'.repeat(26)}.json`)
    // kid in the envelope is the same derivation the key store uses.
    expect(kidForPub(Buffer.alloc(32))).toMatch(/^ok_[0-9a-f]{16}$/)
  })
})

describe('T-6: a conductor scope binds every target to a live Claude CLI session', () => {
  // MUTATION: the `target_not_bound` refusal in plan() removed. A gate grant then signed with
  // claude_session_id null (any hook with an overridden HERMES_SESSION_ID accepts it) and this failed.
  test('a scope with any unbound target is refused before the dialog; nothing is written', async () => {
    const base = readyStore()
    const p = ports(base)
    const targets = [
      { profile: 'default', session_id: 'w-1', claude_session_id: 'c-1', backend: 'bk_a' },
      { profile: 'default', session_id: 'w-2', claude_session_id: null, backend: 'bk_a' }
    ]

    await expect(confirmAndSignGrants(request({ targets }), p)).rejects.toMatchObject({ code: 'target_not_bound' })
    expect(p.confirm).not.toHaveBeenCalled()
    expect(listGrants(base.grantsDir)).toEqual([])
  })

  test('a quote-only decision may still sign null (nothing for a hook to accept)', async () => {
    const base = readyStore()
    const targets = [{ profile: 'default', session_id: 'w-2', claude_session_id: null, backend: 'bk_a' }]
    const out = await confirmAndSignGrants(request({ targets, scope: [] }), ports(base))

    expect(out.cancelled).toBe(false)
    expect(payloadOf((out as SignedOutcome).grants[0].envelope).targets).toEqual([{ session_id: 'w-2', claude_session_id: null }])
  })
})
