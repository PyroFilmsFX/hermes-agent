/**
 * #60 U10: the Electron owner key store (VERIFY addendum §1.2, §1.4 step 4, E-5, E-6).
 *
 * safeStorage is ALWAYS a mock here: these tests never touch the real Keychain. Every key
 * directory is a fresh mkdtemp under os.tmpdir(), never ~/.hermes.
 */

import { createHash, createPublicKey, verify as edVerify } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { inspect } from 'node:util'

import { afterEach, describe, expect, test } from 'vitest'

import {
  createOwnerKeyStore,
  defaultOwnerGrantsDir,
  defaultOwnerKeyDir,
  GRANT_DOMAIN_PREFIX,
  kidForPub,
  OWNER_KEY_FILE,
  OwnerKeyError,
  type SafeStorageLike
} from './owner-grant-key'

const tmpDirs: string[] = []

function tmpKeyDir(): string {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'ogk-'))
  tmpDirs.push(base)

  return path.join(base, 'owner-grants', '.key')
}

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

/** A reversible, non-identity stand-in for the Keychain wrap. Records every plaintext it saw
 *  so E-6 can search everything else for those exact bytes. */
function mockSafeStorage(opts: { available?: boolean } = {}) {
  const plaintexts: string[] = []
  const calls: string[] = []

  const api: SafeStorageLike = {
    isEncryptionAvailable: () => {
      calls.push('isEncryptionAvailable')

      return opts.available !== false
    },
    encryptString: (plain: string) => {
      calls.push('encryptString')
      plaintexts.push(plain)

      return Buffer.from('MOCKWRAP:' + Buffer.from(plain, 'utf8').reverse().toString('base64'), 'utf8')
    },
    decryptString: (wrapped: Buffer) => {
      calls.push('decryptString')
      const text = wrapped.toString('utf8')

      if (!text.startsWith('MOCKWRAP:')) {
        throw new Error('mock keychain: not a blob this mock wrapped')
      }

      return Buffer.from(text.slice('MOCKWRAP:'.length), 'base64').reverse().toString('utf8')
    }
  }

  return { api, plaintexts, calls }
}

function captureLog() {
  const lines: string[] = []

  const log = (level: string, message: string, meta?: unknown) => {
    lines.push(`${level} ${message} ${meta === undefined ? '' : JSON.stringify(meta)}`)
  }

  return { log, lines }
}

function anchorFor(pubB64url: string, status = 'active') {
  const pub = Buffer.from(pubB64url, 'base64url')

  return { keys: [{ kid: kidForPub(pub), pub: pubB64url, status }] }
}

describe('E-5: the owner key persists across restarts and refuses signing on an anchor mismatch', () => {
  test('E-5a ensure() generates once, wraps with safeStorage, writes 0600 in a 0700 dir, and a restart loads the same key', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    const first = createOwnerKeyStore({ safeStorage: ss.api, keyDir })
    const info = first.ensure()

    expect(info.kid).toMatch(/^ok_[0-9a-f]{16}$/)
    expect(info.kid).toBe(kidForPub(Buffer.from(info.pub, 'base64url')))
    const blobPath = path.join(keyDir, OWNER_KEY_FILE)
    expect(fs.statSync(blobPath).mode & 0o777).toBe(0o600)
    expect(fs.statSync(keyDir).mode & 0o777).toBe(0o700)
    const blobBytes = fs.readFileSync(blobPath)

    // A second ensure() in the same run is a no-op.
    expect(first.ensure()).toEqual(info)

    // "Restart": a brand-new store over the same dir and the same Keychain.
    const second = createOwnerKeyStore({ safeStorage: ss.api, keyDir })
    expect(second.loadIfEnrolled()).toEqual(info)
    expect(second.ensure()).toEqual(info)
    expect(fs.readFileSync(blobPath).equals(blobBytes)).toBe(true)
    // Exactly one wrap ever happened: the key was generated once.
    expect(ss.calls.filter(c => c === 'encryptString')).toHaveLength(1)
  })

  test('E-5b loadIfEnrolled() on a fresh machine returns null without touching the Keychain', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir })

    expect(store.loadIfEnrolled()).toBeNull()
    expect(store.publicInfo()).toBeNull()
    expect(ss.calls).toEqual([])
    expect(fs.existsSync(keyDir)).toBe(false)
  })

  test('E-5c no Keychain encryption means no key: never a plaintext fallback, nothing written', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage({ available: false })
    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir })

    expect(() => store.ensure()).toThrowError(expect.objectContaining({ code: 'keychain_unavailable' }))
    expect(fs.existsSync(path.join(keyDir, OWNER_KEY_FILE))).toBe(false)
    expect(ss.calls).not.toContain('encryptString')
  })

  test('E-5d conductor-scoped signing needs an anchor whose active key is this key; quote-only forwards still sign', () => {
    const keyDir = tmpKeyDir()
    const store = createOwnerKeyStore({ safeStorage: mockSafeStorage().api, keyDir })
    const info = store.ensure()
    const payload = Buffer.from('{"v":1}')
    const scoped = ['conductor:gate:review-budget-enable']

    // No anchor installed yet.
    expect(store.anchorState()).toBe('missing')
    expect(() => store.assertMaySign(scoped)).toThrowError(expect.objectContaining({ code: 'anchor_missing' }))
    expect(() => store.assertMaySign([])).not.toThrow()

    // Anchor pins someone else's key (a phished admin prompt, or a swapped blob).
    const other = createOwnerKeyStore({ safeStorage: mockSafeStorage().api, keyDir: tmpKeyDir() }).ensure()
    expect(store.setAnchor(anchorFor(other.pub))).toBe('mismatch')
    expect(() => store.assertMaySign(scoped)).toThrowError(expect.objectContaining({ code: 'anchor_mismatch' }))
    expect(() => store.signEnvelope(payload, scoped)).toThrowError(expect.objectContaining({ code: 'anchor_mismatch' }))
    expect(store.signEnvelope(payload, []).kid).toBe(info.kid)

    // A retired or revoked entry for our kid is not "active": still a mismatch.
    expect(store.setAnchor(anchorFor(info.pub, 'retired'))).toBe('mismatch')
    expect(store.setAnchor(anchorFor(info.pub, 'revoked'))).toBe('mismatch')

    // The matching anchor enables conductor scopes.
    expect(store.setAnchor(anchorFor(info.pub))).toBe('match')
    expect(() => store.assertMaySign(scoped)).not.toThrow()
    expect(store.signEnvelope(payload, scoped).kid).toBe(info.kid)
  })

  test('E-5e a blob whose recorded public key differs from the unwrapped private key is refused (swap detection)', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    createOwnerKeyStore({ safeStorage: ss.api, keyDir }).ensure()
    const blobPath = path.join(keyDir, OWNER_KEY_FILE)
    const doc = JSON.parse(fs.readFileSync(blobPath, 'utf8'))
    const other = createOwnerKeyStore({ safeStorage: ss.api, keyDir: tmpKeyDir() }).ensure()
    doc.pub = other.pub
    doc.kid = other.kid
    fs.writeFileSync(blobPath, JSON.stringify(doc), { mode: 0o600 })

    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir })
    expect(() => store.loadIfEnrolled()).toThrowError(expect.objectContaining({ code: 'key_blob_inconsistent' }))
    expect(store.publicInfo()).toBeNull()
    expect(() => store.signEnvelope(Buffer.from('{}'), [])).toThrowError(expect.objectContaining({ code: 'no_key' }))
  })

  test('E-5f a damaged blob is never overwritten by a fresh key: ensure() fails closed', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    createOwnerKeyStore({ safeStorage: ss.api, keyDir }).ensure()
    const blobPath = path.join(keyDir, OWNER_KEY_FILE)
    fs.writeFileSync(blobPath, '{"format":"hermes-owner-key/v1","wrapped":"garbage"}', { mode: 0o600 })
    const before = fs.readFileSync(blobPath)

    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir })
    expect(() => store.ensure()).toThrow(OwnerKeyError)
    expect(fs.readFileSync(blobPath).equals(before)).toBe(true)
  })

  test('E-5g a symlinked blob, or a blob owned by another uid, is refused', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    createOwnerKeyStore({ safeStorage: ss.api, keyDir }).ensure()
    const blobPath = path.join(keyDir, OWNER_KEY_FILE)
    const moved = blobPath + '.real'
    fs.renameSync(blobPath, moved)
    fs.symlinkSync(moved, blobPath)

    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir })
    expect(() => store.loadIfEnrolled()).toThrowError(expect.objectContaining({ code: 'key_blob_untrusted' }))

    fs.unlinkSync(blobPath)
    fs.renameSync(moved, blobPath)
    const foreign = createOwnerKeyStore({ safeStorage: ss.api, keyDir, uid: (process.getuid?.() ?? 0) + 1 })
    expect(() => foreign.loadIfEnrolled()).toThrowError(expect.objectContaining({ code: 'key_blob_untrusted' }))
  })

  test('E-5h a loose blob mode is tightened back to 0600 on load (it is ciphertext, so fail-safe, not fatal)', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    const info = createOwnerKeyStore({ safeStorage: ss.api, keyDir }).ensure()
    const blobPath = path.join(keyDir, OWNER_KEY_FILE)
    fs.chmodSync(blobPath, 0o644)
    const log = captureLog()

    expect(createOwnerKeyStore({ safeStorage: ss.api, keyDir, log: log.log }).loadIfEnrolled()).toEqual(info)
    expect(fs.statSync(blobPath).mode & 0o777).toBe(0o600)
    expect(log.lines.join('\n')).toMatch(/tightened/)
  })

  test('E-5i signEnvelope signs DOMAIN_PREFIX + exact payload bytes with the stored key (Node verify)', () => {
    const keyDir = tmpKeyDir()
    const store = createOwnerKeyStore({ safeStorage: mockSafeStorage().api, keyDir })
    const info = store.ensure()
    const payload = Buffer.from('{"text":"héllo 👋","v":1}', 'utf8')
    const env = store.signEnvelope(payload, [])

    expect(Object.keys(env).sort()).toEqual(['format', 'kid', 'payload', 'sig'])
    expect(env.format).toBe('hermes-owner-grant/v1')
    expect(Buffer.from(env.payload, 'base64url').equals(payload)).toBe(true)
    const pubKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: info.pub }, format: 'jwk' })
    const signed = Buffer.concat([GRANT_DOMAIN_PREFIX, payload])
    expect(edVerify(null, signed, pubKey, Buffer.from(env.sig, 'base64url'))).toBe(true)
    // Without the domain prefix the same signature must not verify.
    expect(edVerify(null, payload, pubKey, Buffer.from(env.sig, 'base64url'))).toBe(false)
  })

  test('E-5j kid is ok_ + first 16 hex of sha256(raw 32-byte pub), the formula of envelope.kid_for_pub', () => {
    const pub = Buffer.alloc(32, 7)
    expect(kidForPub(pub)).toBe('ok_' + createHash('sha256').update(pub).digest('hex').slice(0, 16))
    expect(() => kidForPub(Buffer.alloc(31))).toThrow()
  })

  test('E-5k default paths follow the real home (os.userInfo), never HERMES_HOME', () => {
    const home = os.userInfo().homedir
    expect(defaultOwnerGrantsDir()).toBe(path.join(home, '.hermes', 'owner-grants'))
    expect(defaultOwnerKeyDir()).toBe(path.join(home, '.hermes', 'owner-grants', '.key'))
    expect(defaultOwnerKeyDir('/Users/x')).toBe('/Users/x/.hermes/owner-grants/.key')
  })
})

describe('E-6: the private key never appears in IPC, env, logs or a grant', () => {
  function forbiddenStrings(plaintexts: string[], wrappedBlob: string): string[] {
    const out = new Set<string>()

    for (const plain of plaintexts) {
      const der = Buffer.from(plain, 'base64')
      const seed = der.subarray(der.length - 32)
      out.add(plain)
      out.add(der.toString('hex'))
      out.add(der.toString('base64url'))
      out.add(seed.toString('hex'))
      out.add(seed.toString('base64'))
      out.add(seed.toString('base64url'))
    }

    out.add(wrappedBlob)

    return [...out].filter(s => s.length >= 16)
  }

  test('E-6a no public surface, log line, error, env entry or envelope carries private or wrapped key bytes', () => {
    const keyDir = tmpKeyDir()
    const ss = mockSafeStorage()
    const log = captureLog()
    const envBefore = JSON.stringify(process.env)
    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir, log: log.log })
    store.ensure()
    const restarted = createOwnerKeyStore({ safeStorage: ss.api, keyDir, log: log.log })
    restarted.loadIfEnrolled()
    restarted.setAnchor(null)
    const errors: string[] = []

    for (const scopes of [['conductor:prod:target'], ['conductor:gate:review-budget-enable']]) {
      try {
        restarted.signEnvelope(Buffer.from('{"v":1}'), scopes)
      } catch (error) {
        errors.push(String(error), JSON.stringify(error), inspect(error, { showHidden: true, depth: 8 }))
      }
    }

    expect(errors.length).toBeGreaterThan(0)
    const envelope = restarted.signEnvelope(Buffer.from('{"v":1,"text":"ok"}'), [])
    const wrapped = JSON.parse(fs.readFileSync(path.join(keyDir, OWNER_KEY_FILE), 'utf8')).wrapped
    const forbidden = forbiddenStrings(ss.plaintexts, wrapped)
    expect(ss.plaintexts).toHaveLength(1)

    const surfaces: Record<string, string> = {
      json: JSON.stringify(restarted),
      jsonFirst: JSON.stringify(store),
      inspect: inspect(restarted, { showHidden: true, depth: 8, getters: true }),
      inspectFirst: inspect(store, { showHidden: true, depth: 8, getters: true }),
      keys: JSON.stringify([Object.getOwnPropertyNames(restarted), Object.getOwnPropertySymbols(restarted).map(String)]),
      publicInfo: JSON.stringify(restarted.publicInfo()),
      envValue: String(restarted.grantKeysEnvValue()),
      ipcStatus: JSON.stringify(restarted.statusForIpc()),
      envelope: JSON.stringify(envelope),
      envelopePayload: Buffer.from(envelope.payload, 'base64url').toString('utf8'),
      logs: log.lines.join('\n'),
      errors: errors.join('\n'),
      env: JSON.stringify(process.env)
    }

    for (const [name, text] of Object.entries(surfaces)) {
      for (const secret of forbidden) {
        expect(text.includes(secret), `${name} leaks key material`).toBe(false)
      }
    }

    // The env value is exactly <kid>:<b64url pub>, the public HERMES_OWNER_GRANT_KEYS entry.
    const info = restarted.publicInfo()!
    expect(restarted.grantKeysEnvValue()).toBe(`${info.kid}:${info.pub}`)
    // The store never writes the environment.
    expect(JSON.stringify(process.env)).toBe(envBefore)
    // The blob on disk is the wrap, not the plaintext key.
    const onDisk = fs.readFileSync(path.join(keyDir, OWNER_KEY_FILE), 'utf8')

    for (const secret of forbidden.filter(s => s !== wrapped)) {
      expect(onDisk.includes(secret)).toBe(false)
    }
  })

  test('E-6b the module imports no electron IPC and never assigns process.env or logs to console', () => {
    const source = fs.readFileSync(path.join(__dirname, 'owner-grant-key.ts'), 'utf8')
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

    expect(code).not.toMatch(/from ['"]electron['"]/)
    expect(code).not.toMatch(/ipcMain|ipcRenderer|webContents|contextBridge/)
    expect(code).not.toMatch(/process\.env(\.[A-Za-z_]+|\[[^\]]+\])\s*=/)
    expect(code).not.toMatch(/console\./)
  })

  test('E-6c statusForIpc exposes only kid, public key, anchor state and enrollment', () => {
    const store = createOwnerKeyStore({ safeStorage: mockSafeStorage().api, keyDir: tmpKeyDir() })
    expect(store.statusForIpc()).toEqual({ enrolled: false, kid: null, pub: null, anchor: 'missing' })
    const info = store.ensure()
    expect(store.statusForIpc()).toEqual({ enrolled: true, kid: info.kid, pub: info.pub, anchor: 'missing' })
  })
})
