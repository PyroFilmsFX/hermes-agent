import { verify as edVerify, createPublicKey } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import {
  b64urlDecode,
  createOwnerKeyStore,
  type OwnerDomainEnvelope,
  type SafeStorageLike
} from './owner-grant-key'
import {
  createSessionAttestationIssuer,
  ISSUER_BACKOFF_MS,
  type LaunchAttestationPayload,
  type LaunchFeedEntry
} from './session-binding-issuer'
import { createSessionBindingStore } from './session-binding-store'

const tmpDirs: string[] = []

afterEach(() => {
  vi.useRealTimers()
  for (const dir of tmpDirs.splice(0)) {
    try {
      fs.rmSync(dir, { recursive: true, force: true })
    } catch {
      // ignore
    }
  }
})

function tmpDir(prefix = 'sbi-'): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  tmpDirs.push(dir)
  return dir
}

const mockSafeStorage: SafeStorageLike = {
  isEncryptionAvailable: () => true,
  encryptString: (plain: string) => Buffer.from('W:' + Buffer.from(plain).toString('base64')),
  decryptString: (wrapped: Buffer) => Buffer.from(wrapped.toString().slice(2), 'base64').toString()
}

function harness(options: {
  anchorStatus?: 'active' | 'mismatch' | 'none'
  now?: () => number
} = {}) {
  const base = tmpDir()
  const keyStore = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
  const info = keyStore.ensure()

  if (options.anchorStatus !== 'none') {
    if (options.anchorStatus === 'mismatch') {
      keyStore.setAnchor({ keys: [{ kid: 'ok_mismatch123456', pub: info.pub, status: 'active' }] })
    } else {
      keyStore.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })
    }
  }

  const grantsDir = path.join(base, 'grants')
  const bindingStore = createSessionBindingStore({ store: keyStore, grantsDir })

  let currentTime = 1_000_000
  const now = options.now ?? (() => currentTime)

  return {
    base,
    keyStore,
    info,
    grantsDir,
    bindingStore,
    now,
    getTime: () => currentTime,
    setTime: (t: number) => { currentTime = t },
    advanceTime: (ms: number) => { currentTime += ms }
  }
}

describe('session-binding-issuer: launch-attestation issuer (b10 H7b)', () => {
  test('1. bound launch yields an attestation file with every required Python verifier field', async () => {
    const h = harness()
    h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: 'hs_1',
      project_root: '/abs/path/project',
      repo_common_root: '/abs/path/project',
      repo_remote: 'git@github.com:example/repo.git'
    })

    const launch: LaunchFeedEntry = {
      hermes_session_id: 'hs_1',
      claude_session_id: 'cs_1',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false,
      hermes_lineage: ['parent_0']
    }

    const fakeFetch = vi.fn(async (url: string) => {
      if (url.includes('since=0')) {
        return { seq: 1, launches: [launch] }
      }
      return { seq: 1, launches: [] }
    })

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now
    })

    await issuer.pollOnce()

    const attestDir = path.join(h.grantsDir, 'session-attest', 'cs_1')
    expect(fs.existsSync(attestDir)).toBe(true)
    const files = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(files.length).toBe(1)

    const content = JSON.parse(fs.readFileSync(path.join(attestDir, files[0]), 'utf8')) as OwnerDomainEnvelope
    expect(content.format).toBe('hermes-launch-attestation/v1')
    expect(content.kid).toBe(h.info.kid)
    expect(typeof content.payload).toBe('string')
    expect(typeof content.sig).toBe('string')

    const rawPayload = b64urlDecode(content.payload)
    expect(rawPayload).not.toBeNull()
    const payload = JSON.parse(rawPayload!.toString('utf8')) as LaunchAttestationPayload

    // All _REQUIRED_PAYLOAD_FIELDS
    expect(payload.v).toBe(1)
    expect(payload.aud).toEqual(['conductor:session-binding'])
    expect(typeof payload.owner_uid).toBe('number')
    expect(payload.profile).toBe('default')
    expect(payload.backend).toBe('spawn-test-1')
    expect(payload.hermes_session_id).toBe('hs_1')
    expect(payload.claude_session_id).toBe('cs_1')
    expect(payload.launch_seq).toBe(1)
    expect(payload.project_root).toBe('/abs/path/project')
    expect(typeof payload.binding_nonce).toBe('string')
    expect(payload.binding_nonce!.length).toBeGreaterThan(0)
    expect(payload.issued_at).toBe(h.getTime())
    expect(payload.expires_at).toBe(h.getTime() + 30 * 60 * 1000)

    // Optional fields
    expect(payload.repo_common_root).toBe('/abs/path/project')
    expect(payload.binding_seq).toBe(1)
    expect(payload.repo_remote).toBe('git@github.com:example/repo.git')
    expect(payload.hermes_lineage).toEqual(['parent_0'])

    // Ed25519 signature validity
    const pubKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: h.info.pub }, format: 'jwk' })
    const signPrefix = Buffer.concat([Buffer.from('hermes-launch-attestation/v1\0', 'ascii'), rawPayload!])
    const validSig = edVerify(null, signPrefix, pubKey, Buffer.from(content.sig, 'base64url'))
    expect(validSig).toBe(true)

    expect(issuer.isAttestationLive('default', 'hs_1')).toBe(true)
    issuer.dispose()
  })

  test('2. unbound launch yields attestation with null binding fields', async () => {
    const h = harness()

    const launch: LaunchFeedEntry = {
      hermes_session_id: 'hs_unbound',
      claude_session_id: 'cs_unbound',
      profile: 'default',
      launch_seq: 2, resumed_unverified: false
    }

    const fakeFetch = vi.fn(async () => ({ seq: 2, launches: [launch] }))

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now
    })

    await issuer.pollOnce()

    const attestDir = path.join(h.grantsDir, 'session-attest', 'cs_unbound')
    expect(fs.existsSync(attestDir)).toBe(true)
    const files = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(files.length).toBe(1)

    const content = JSON.parse(fs.readFileSync(path.join(attestDir, files[0]), 'utf8')) as OwnerDomainEnvelope
    const rawPayload = b64urlDecode(content.payload)!
    const payload = JSON.parse(rawPayload.toString('utf8')) as LaunchAttestationPayload

    expect(payload.project_root).toBeNull()
    expect(payload.binding_nonce).toBeNull()
    expect(payload.repo_common_root).toBeNull()
    expect(payload.binding_seq).toBeNull()
    expect(payload.repo_remote).toBeNull()
    expect(payload.v).toBe(1)
    expect(payload.aud).toEqual(['conductor:session-binding'])
    expect(payload.hermes_session_id).toBe('hs_unbound')
    expect(payload.claude_session_id).toBe('cs_unbound')

    expect(issuer.isAttestationLive('default', 'hs_unbound')).toBe(false)
    issuer.dispose()
  })

  test('3. TTL is 30 min, refresh at 15 min, and stops refreshing when launch disappears', async () => {
    vi.useFakeTimers()
    const h = harness()
    h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: 'hs_refresh',
      project_root: '/abs/path/refresh',
      repo_common_root: '/abs/path/refresh'
    })

    const launch: LaunchFeedEntry = {
      hermes_session_id: 'hs_refresh',
      claude_session_id: 'cs_refresh',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false
    }

    let activeInFeed = true
    const fakeFetch = vi.fn(async (url: string) => {
      if (!activeInFeed) {
        return { seq: 3, launches: [] }
      }
      if (url.includes('since=0')) {
        return { seq: 1, launches: [launch] }
      }
      return { seq: 1, launches: [] }
    })

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now
    })

    await issuer.pollOnce()

    const attestDir = path.join(h.grantsDir, 'session-attest', 'cs_refresh')
    const files1 = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(files1.length).toBe(1)

    // Advance 15 min (900_000 ms) -> triggers refresh
    h.advanceTime(15 * 60 * 1000)
    await vi.advanceTimersByTimeAsync(15 * 60 * 1000)

    const files2 = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(files2.length).toBe(2)

    // Second file has updated issued_at and expires_at
    files2.sort()
    const secondContent = JSON.parse(fs.readFileSync(path.join(attestDir, files2[1]), 'utf8')) as OwnerDomainEnvelope
    const secondPayload = JSON.parse(b64urlDecode(secondContent.payload)!.toString('utf8'))
    expect(secondPayload.issued_at).toBe(1_000_000 + 15 * 60 * 1000)
    expect(secondPayload.expires_at).toBe(secondPayload.issued_at + 30 * 60 * 1000)

    // Launch disappears from feed
    activeInFeed = false

    // Advance another 15 min -> refresh should check feed and stop
    h.advanceTime(15 * 60 * 1000)
    await vi.advanceTimersByTimeAsync(15 * 60 * 1000)

    const files3 = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(files3.length).toBe(2) // No third file written!

    issuer.dispose()
  })

  test('4. revoke immediately re-issues with null binding fields', async () => {
    const h = harness()
    h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: 'hs_revoke',
      project_root: '/abs/path/revoke',
      repo_common_root: '/abs/path/revoke'
    })

    const launch: LaunchFeedEntry = {
      hermes_session_id: 'hs_revoke',
      claude_session_id: 'cs_revoke',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false
    }

    const fakeFetch = vi.fn(async () => ({ seq: 1, launches: [launch] }))

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now
    })

    await issuer.pollOnce()
    expect(issuer.isAttestationLive('default', 'hs_revoke')).toBe(true)

    const attestDir = path.join(h.grantsDir, 'session-attest', 'cs_revoke')
    const filesBefore = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(filesBefore.length).toBe(1)

    // Advance time slightly
    h.advanceTime(5000)

    await issuer.revoke('default', 'hs_revoke')

    expect(issuer.isAttestationLive('default', 'hs_revoke')).toBe(false)

    const filesAfter = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(filesAfter.length).toBe(2)
    filesAfter.sort()

    const latest = JSON.parse(fs.readFileSync(path.join(attestDir, filesAfter[1]), 'utf8')) as OwnerDomainEnvelope
    const latestPayload = JSON.parse(b64urlDecode(latest.payload)!.toString('utf8'))
    expect(latestPayload.project_root).toBeNull()
    expect(latestPayload.binding_nonce).toBeNull()
    expect(latestPayload.repo_common_root).toBeNull()
    expect(latestPayload.binding_seq).toBeNull()
    expect(latestPayload.issued_at).toBe(h.getTime())

    issuer.dispose()
  })

  test('5. isAttestationLive covers true and all false cases', async () => {
    const h = harness()
    h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: 'hs_live',
      project_root: '/abs/path/live',
      repo_common_root: '/abs/path/live'
    })

    const launch: LaunchFeedEntry = {
      hermes_session_id: 'hs_live',
      claude_session_id: 'cs_live',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false
    }

    const fakeFetch = vi.fn(async () => ({ seq: 1, launches: [launch] }))

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now
    })

    // Before launch processed
    expect(issuer.isAttestationLive('default', 'hs_live')).toBe(false)
    expect(issuer.isAttestationLive('default', 'other_session')).toBe(false)
    expect(issuer.isAttestationLive('other_profile', 'hs_live')).toBe(false)

    await issuer.pollOnce()
    // Now live
    expect(issuer.isAttestationLive('default', 'hs_live')).toBe(true)

    // Advance time past 30 min (expired)
    h.advanceTime(31 * 60 * 1000)
    expect(issuer.isAttestationLive('default', 'hs_live')).toBe(false)

    issuer.dispose()
  })

  test('6. backoff on fetch errors follows 1s -> 2s -> 5s -> 15s', async () => {
    vi.useFakeTimers()
    const h = harness()
    let calls = 0
    const delaysRecorded: number[] = []

    const fakeFetch = vi.fn(async () => {
      calls++
      throw new Error(`fetch error ${calls}`)
    })

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now,
      log: (msg) => {
        const match = /backing off (\d+)ms/.exec(msg)
        if (match) {
          delaysRecorded.push(Number(match[1]))
        }
      }
    })

    issuer.start()

    // 1st error -> backs off 1s (1000ms)
    await vi.advanceTimersByTimeAsync(10)
    expect(delaysRecorded[0]).toBe(ISSUER_BACKOFF_MS[0]) // 1000

    // 2nd error -> backs off 2s (2000ms)
    await vi.advanceTimersByTimeAsync(1000)
    expect(delaysRecorded[1]).toBe(ISSUER_BACKOFF_MS[1]) // 2000

    // 3rd error -> backs off 5s (5000ms)
    await vi.advanceTimersByTimeAsync(2000)
    expect(delaysRecorded[2]).toBe(ISSUER_BACKOFF_MS[2]) // 5000

    // 4th error -> backs off 15s (15000ms)
    await vi.advanceTimersByTimeAsync(5000)
    expect(delaysRecorded[3]).toBe(ISSUER_BACKOFF_MS[3]) // 15000

    issuer.dispose()
  })

  test('7. signing-off idles without throwing when anchor missing or mismatched', async () => {
    const h = harness({ anchorStatus: 'mismatch' })
    h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: 'hs_unanchored',
      project_root: '/abs/path/unanchored',
      repo_common_root: '/abs/path/unanchored'
    })

    const launch: LaunchFeedEntry = {
      hermes_session_id: 'hs_unanchored',
      claude_session_id: 'cs_unanchored',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false
    }

    const fakeFetch = vi.fn(async () => ({ seq: 1, launches: [launch] }))

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: fakeFetch,
      now: h.now
    })

    // Must not throw
    await expect(issuer.pollOnce()).resolves.toBeUndefined()

    expect(issuer.isAttestationLive('default', 'hs_unanchored')).toBe(false)
    const attestDir = path.join(h.grantsDir, 'session-attest', 'cs_unanchored')
    expect(fs.existsSync(attestDir)).toBe(false)

    issuer.dispose()
  })

  test('8. crosslang fixture: issued envelope matches the committed vector shape (regen with HERMES_REGEN_VECTORS=1)', async () => {
    const h = harness()
    h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: '20260929_issuer_session',
      project_root: '/Users/justin/Documents/Projects/Business/hermes-cntrl',
      repo_common_root: '/Users/justin/Documents/Projects/Business/hermes-cntrl',
      repo_remote: 'git@github.com:nousresearch/hermes-agent.git'
    })

    const launch: LaunchFeedEntry = {
      hermes_session_id: '20260929_issuer_session',
      claude_session_id: 'claude_session_crosslang_1',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false,
      hermes_lineage: ['parent_lineage_0']
    }

    const fakeFetch = vi.fn(async () => ({ seq: 1, launches: [launch] }))

    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-crosslang-1',
      fetchJson: fakeFetch,
      now: () => 1_790_000_000_000
    })

    await issuer.pollOnce()

    const attestDir = path.join(h.grantsDir, 'session-attest', 'claude_session_crosslang_1')
    const files = fs.readdirSync(attestDir).filter(f => f.endsWith('.json'))
    expect(files.length).toBe(1)

    const content = JSON.parse(fs.readFileSync(path.join(attestDir, files[0]), 'utf8')) as OwnerDomainEnvelope
    const rawPayload = b64urlDecode(content.payload)!
    const payload = JSON.parse(rawPayload.toString('utf8')) as LaunchAttestationPayload

    const vectorData = {
      schema: 'hermes-owner-grant-attest-vector/v1',
      anchor_key: {
        alg: 'Ed25519',
        kid: h.info.kid,
        not_before: 0,
        pub: h.info.pub,
        retired_at: null,
        status: 'active'
      },
      owner_uid: payload.owner_uid,
      claude_session_id: payload.claude_session_id,
      hermes_session_id: payload.hermes_session_id,
      project_root: payload.project_root,
      binding_nonce: payload.binding_nonce,
      issued_at: payload.issued_at,
      expires_at: payload.expires_at,
      envelope: content
    }

    // The committed vector is the Python side's golden input: rewrite it only on request
    // (HERMES_REGEN_VECTORS=1), never as a side effect of a normal test run.
    const targetVectorPath = path.resolve(__dirname, '../../../tests/hermes_owner_grant/fixtures/attest_issuer_vector.json')
    if (process.env.HERMES_REGEN_VECTORS === '1') {
      fs.writeFileSync(targetVectorPath, JSON.stringify(vectorData, null, 2) + '\n', 'utf8')
    }
    const committed = JSON.parse(fs.readFileSync(targetVectorPath, 'utf8'))
    expect(committed.schema).toBe(vectorData.schema)
    expect(Object.keys(committed).sort()).toEqual(Object.keys(vectorData).sort())
    expect(Object.keys(committed.envelope).sort()).toEqual(Object.keys(vectorData.envelope).sort())
    expect(committed.envelope.format).toBe(vectorData.envelope.format)

    issuer.dispose()
  })
})

describe('session-binding-issuer: b10 review — binding changes, compression and stale refreshes', () => {
  /** Reads every attestation for a Claude sid, oldest first (file names are `<issued_at>-<seq>`). */
  function payloadsFor(grantsDir: string, cid: string): LaunchAttestationPayload[] {
    const dir = path.join(grantsDir, 'session-attest', cid)
    if (!fs.existsSync(dir)) return []
    return fs
      .readdirSync(dir)
      .filter(f => f.endsWith('.json'))
      .sort((a, b) => Number(a.split('-')[0]) - Number(b.split('-')[0]))
      .map(f => {
        const env = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')) as OwnerDomainEnvelope
        return JSON.parse(b64urlDecode(env.payload)!.toString('utf8')) as LaunchAttestationPayload
      })
  }

  const newest = (grantsDir: string, cid: string) => payloadsFor(grantsDir, cid).at(-1)!

  /** Manual timers: refresh callbacks are fired by the test, never by the clock. */
  function manualTimers() {
    const pending = new Map<number, () => void>()
    let id = 0
    return {
      setTimeout: ((fn: () => void) => {
        id += 1
        pending.set(id, fn)
        return id
      }) as unknown as typeof setTimeout,
      clearTimeout: ((handle: number) => {
        pending.delete(handle)
      }) as unknown as typeof clearTimeout,
      /** Fire every armed timer once (the refresh). */
      fireAll: () => {
        const fns = Array.from(pending.values())
        pending.clear()
        for (const fn of fns) fn()
      }
    }
  }

  function deferred<T>() {
    let resolve!: (v: T) => void
    const promise = new Promise<T>(r => {
      resolve = r
    })
    return { promise, resolve }
  }

  test('3a. binding an already-running unbound session re-issues its attestation at once (onBindingChanged)', async () => {
    const h = harness()
    const launch: LaunchFeedEntry = { hermes_session_id: 'hs_late', claude_session_id: 'cs_late', profile: 'default', launch_seq: 1, resumed_unverified: false }
    const timers = manualTimers()
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => ({ seq: 1, launches: [launch] })),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    expect(newest(h.grantsDir, 'cs_late').project_root).toBeNull()
    expect(issuer.isAttestationLive('default', 'hs_late')).toBe(false)

    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_late', project_root: '/abs/late', repo_common_root: '/abs/late' })
    h.advanceTime(1000)
    await issuer.onBindingChanged('default', 'hs_late')

    const after = newest(h.grantsDir, 'cs_late')
    expect(after.project_root).toBe('/abs/late')
    expect(after.binding_nonce).toBe(h.bindingStore.get('default', 'hs_late')!.binding_nonce)
    expect(issuer.isAttestationLive('default', 'hs_late')).toBe(true)
    issuer.dispose()
  })

  test('3b. a refresh derives the binding from the current store, even for a launch cached as unbound', async () => {
    const h = harness()
    const launch: LaunchFeedEntry = { hermes_session_id: 'hs_rf', claude_session_id: 'cs_rf', profile: 'default', launch_seq: 1, resumed_unverified: false }
    const timers = manualTimers()
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => ({ seq: 1, launches: [launch] })),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    expect(newest(h.grantsDir, 'cs_rf').project_root).toBeNull()

    // Bound behind the issuer's back (no onBindingChanged): the next refresh still picks it up.
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_rf', project_root: '/abs/rf', repo_common_root: '/abs/rf' })
    h.advanceTime(15 * 60 * 1000)
    timers.fireAll()
    await vi.waitFor(() => expect(payloadsFor(h.grantsDir, 'cs_rf')).toHaveLength(2))
    expect(newest(h.grantsDir, 'cs_rf').project_root).toBe('/abs/rf')
    issuer.dispose()
  })

  test('3c. a re-bind moves the live attestation to the new project immediately', async () => {
    const h = harness()
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_mv', project_root: '/abs/old', repo_common_root: '/abs/old' })
    const launch: LaunchFeedEntry = { hermes_session_id: 'hs_mv', claude_session_id: 'cs_mv', profile: 'default', launch_seq: 1, resumed_unverified: false }
    const timers = manualTimers()
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => ({ seq: 1, launches: [launch] })),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    expect(newest(h.grantsDir, 'cs_mv').project_root).toBe('/abs/old')

    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_mv', project_root: '/abs/new', repo_common_root: '/abs/new' })
    h.advanceTime(1000)
    await issuer.onBindingChanged('default', 'hs_mv')

    const after = newest(h.grantsDir, 'cs_mv')
    expect(after.project_root).toBe('/abs/new')
    expect(after.binding_seq).toBe(2)
    issuer.dispose()
  })

  test('4a. after /compress the child launch carries the verified parent binding forward via lineage', async () => {
    const h = harness()
    const parent = h.bindingStore.bind({
      profile: 'default',
      hermes_session_id: 'hs_parent',
      project_root: '/abs/compress',
      repo_common_root: '/abs/compress'
    })
    const child: LaunchFeedEntry = {
      hermes_session_id: 'hs_child',
      claude_session_id: 'cs_compress',
      profile: 'default',
      launch_seq: 2, resumed_unverified: false,
      hermes_lineage: ['hs_root', 'hs_parent']
    }
    const timers = manualTimers()
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => ({ seq: 1, launches: [child] })),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    const p = newest(h.grantsDir, 'cs_compress')
    expect(p.hermes_session_id).toBe('hs_child')
    expect(p.hermes_lineage).toEqual(['hs_root', 'hs_parent'])
    expect(p.project_root).toBe('/abs/compress')
    expect('binding_nonce' in parent && p.binding_nonce).toBe('binding_nonce' in parent && parent.binding_nonce)
    expect(p.binding_seq).toBe(1)
    expect(issuer.isAttestationLive('default', 'hs_child')).toBe(true)
    // The parent's binding powers a live build, so a re-bind of the parent gets the native confirm.
    expect(issuer.isAttestationLive('default', 'hs_parent')).toBe(true)

    // Unbinding the parent revokes the carried binding on the child's live launch.
    h.bindingStore.unbind('default', 'hs_parent')
    h.advanceTime(1000)
    await issuer.revoke('default', 'hs_parent')
    expect(newest(h.grantsDir, 'cs_compress').project_root).toBeNull()
    expect(issuer.isAttestationLive('default', 'hs_parent')).toBe(false)
    issuer.dispose()
  })

  test('4b. an explicit child record (unbound) wins over the parent; a nearer unbound ancestor stops the walk', async () => {
    const h = harness()
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_p', project_root: '/abs/p', repo_common_root: '/abs/p' })
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_c', project_root: '/abs/c', repo_common_root: '/abs/c' })
    h.bindingStore.unbind('default', 'hs_c')
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_root2', project_root: '/abs/root2', repo_common_root: '/abs/root2' })
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_mid2', project_root: '/abs/mid2', repo_common_root: '/abs/mid2' })
    h.bindingStore.unbind('default', 'hs_mid2')

    const explicit: LaunchFeedEntry = {
      hermes_session_id: 'hs_c',
      claude_session_id: 'cs_explicit',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false,
      hermes_lineage: ['hs_p']
    }
    const walk: LaunchFeedEntry = {
      hermes_session_id: 'hs_leaf2',
      claude_session_id: 'cs_walk',
      profile: 'default',
      launch_seq: 1, resumed_unverified: false,
      hermes_lineage: ['hs_root2', 'hs_mid2']
    }
    const otherProfile: LaunchFeedEntry = {
      hermes_session_id: 'hs_leaf3',
      claude_session_id: 'cs_other_profile',
      profile: 'work',
      launch_seq: 1, resumed_unverified: false,
      hermes_lineage: ['hs_p']
    }
    const timers = manualTimers()
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => ({ seq: 1, launches: [explicit, walk, otherProfile] })),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    expect(newest(h.grantsDir, 'cs_explicit').project_root).toBeNull()
    expect(newest(h.grantsDir, 'cs_walk').project_root).toBeNull()
    // Lineage never crosses profiles.
    expect(newest(h.grantsDir, 'cs_other_profile').project_root).toBeNull()
    issuer.dispose()
  })

  test('5a. a refresh in flight across an unbind + revoke cannot re-sign the old binding', async () => {
    const h = harness()
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_race', project_root: '/abs/race', repo_common_root: '/abs/race' })
    const launch: LaunchFeedEntry = { hermes_session_id: 'hs_race', claude_session_id: 'cs_race', profile: 'default', launch_seq: 1, resumed_unverified: false }
    const timers = manualTimers()
    let gate: ReturnType<typeof deferred<{ seq: number; launches: unknown[] }>> | null = null
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => (gate ? gate.promise : { seq: 1, launches: [launch] })),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    expect(newest(h.grantsDir, 'cs_race').project_root).toBe('/abs/race')

    // The refresh starts and blocks on its feed snapshot...
    gate = deferred()
    h.advanceTime(15 * 60 * 1000)
    timers.fireAll()

    // ...the owner unbinds meanwhile...
    h.bindingStore.unbind('default', 'hs_race')
    h.advanceTime(1000)
    await issuer.revoke('default', 'hs_race')
    const revokedCount = payloadsFor(h.grantsDir, 'cs_race').length
    expect(newest(h.grantsDir, 'cs_race').project_root).toBeNull()

    // ...then the stale refresh resumes: it must write nothing.
    h.advanceTime(1000)
    gate.resolve({ seq: 1, launches: [launch] })
    await new Promise(resolve => setImmediate(resolve))
    expect(payloadsFor(h.grantsDir, 'cs_race')).toHaveLength(revokedCount)
    expect(newest(h.grantsDir, 'cs_race').project_root).toBeNull()
    expect(issuer.isAttestationLive('default', 'hs_race')).toBe(false)
    issuer.dispose()
  })

  test('5b. a refresh in flight across a compression update cannot overwrite the child with the parent identity', async () => {
    const h = harness()
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_old', project_root: '/abs/cmp', repo_common_root: '/abs/cmp' })
    const parentLaunch: LaunchFeedEntry = { hermes_session_id: 'hs_old', claude_session_id: 'cs_cmp', profile: 'default', launch_seq: 1, resumed_unverified: false }
    const childLaunch: LaunchFeedEntry = {
      hermes_session_id: 'hs_new',
      claude_session_id: 'cs_cmp',
      profile: 'default',
      launch_seq: 2, resumed_unverified: false,
      hermes_lineage: ['hs_old']
    }
    const timers = manualTimers()
    let feed: { seq: number; launches: unknown[] } = { seq: 1, launches: [parentLaunch] }
    let gate: ReturnType<typeof deferred<{ seq: number; launches: unknown[] }>> | null = null
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async (url: string) => (gate && url.includes('since=0&wait=0') ? gate.promise : feed)),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    expect(newest(h.grantsDir, 'cs_cmp').hermes_session_id).toBe('hs_old')

    // Refresh blocks; meanwhile the feed delivers the /compress child for the same Claude sid.
    gate = deferred()
    h.advanceTime(15 * 60 * 1000)
    timers.fireAll()
    feed = { seq: 2, launches: [childLaunch] }
    h.advanceTime(1000)
    await issuer.pollOnce()
    const afterChild = payloadsFor(h.grantsDir, 'cs_cmp').length
    expect(newest(h.grantsDir, 'cs_cmp').hermes_session_id).toBe('hs_new')
    expect(newest(h.grantsDir, 'cs_cmp').project_root).toBe('/abs/cmp')

    // The stale refresh resumes with the old snapshot: discarded, the child stays newest.
    h.advanceTime(1000)
    gate.resolve({ seq: 1, launches: [parentLaunch] })
    await new Promise(resolve => setImmediate(resolve))
    expect(payloadsFor(h.grantsDir, 'cs_cmp')).toHaveLength(afterChild)
    expect(newest(h.grantsDir, 'cs_cmp').hermes_session_id).toBe('hs_new')
    expect(issuer.getLiveLaunches().get('cs_cmp')?.hermes_session_id).toBe('hs_new')
    issuer.dispose()
  })

  test('5c. a refresh re-signs from the feed view: a compression seen only in the snapshot moves to the child', async () => {
    const h = harness()
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_snap_old', project_root: '/abs/snap', repo_common_root: '/abs/snap' })
    const parentLaunch: LaunchFeedEntry = { hermes_session_id: 'hs_snap_old', claude_session_id: 'cs_snap', profile: 'default', launch_seq: 1, resumed_unverified: false }
    const childLaunch: LaunchFeedEntry = {
      hermes_session_id: 'hs_snap_new',
      claude_session_id: 'cs_snap',
      profile: 'default',
      launch_seq: 2, resumed_unverified: false,
      hermes_lineage: ['hs_snap_old']
    }
    const timers = manualTimers()
    let feed: { seq: number; launches: unknown[] } = { seq: 1, launches: [parentLaunch] }
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => feed),
      now: h.now,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    })

    await issuer.pollOnce()
    feed = { seq: 2, launches: [childLaunch] }
    h.advanceTime(15 * 60 * 1000)
    timers.fireAll()
    await vi.waitFor(() => expect(payloadsFor(h.grantsDir, 'cs_snap')).toHaveLength(2))
    const p = newest(h.grantsDir, 'cs_snap')
    expect(p.hermes_session_id).toBe('hs_snap_new')
    expect(p.project_root).toBe('/abs/snap')
    issuer.dispose()
  })
})

describe('session-binding-issuer: b10 fix B — attest only launches with resumed_unverified === false', () => {
  function attestCount(grantsDir: string, cid: string): number {
    const dir = path.join(grantsDir, 'session-attest', cid)
    return fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => f.endsWith('.json')).length : 0
  }

  function gateHarness(launches: unknown[]) {
    const h = harness()
    h.bindingStore.bind({ profile: 'default', hermes_session_id: 'hs_gate', project_root: '/abs/gate', repo_common_root: '/abs/gate' })
    const pending: Array<() => void> = []
    const issuer = createSessionAttestationIssuer({
      bindingStore: h.bindingStore,
      keyStore: h.keyStore,
      grantsDir: h.grantsDir,
      backend: 'spawn-test-1',
      fetchJson: vi.fn(async () => ({ seq: 1, launches })),
      now: h.now,
      setTimeout: ((fn: () => void) => {
        pending.push(fn)
        return pending.length
      }) as unknown as typeof setTimeout,
      clearTimeout: (() => undefined) as unknown as typeof clearTimeout
    })
    return { h, issuer }
  }

  const base = { hermes_session_id: 'hs_gate', profile: 'default', launch_seq: 1 }

  test('resumed_unverified: false signs (fresh, or resumed with a verified prior attestation)', async () => {
    const { h, issuer } = gateHarness([
      { ...base, claude_session_id: 'cs_fresh', sid_origin: 'fresh', resumed_unverified: false },
      { ...base, claude_session_id: 'cs_resumed_ok', sid_origin: 'resumed', resumed_unverified: false }
    ])
    await issuer.pollOnce()
    expect(attestCount(h.grantsDir, 'cs_fresh')).toBe(1)
    expect(attestCount(h.grantsDir, 'cs_resumed_ok')).toBe(1)
    expect(issuer.isAttestationLive('default', 'hs_gate')).toBe(true)
    issuer.dispose()
  })

  test('resumed_unverified true, missing, or non-boolean never signs', async () => {
    const { h, issuer } = gateHarness([
      { ...base, claude_session_id: 'cs_true', sid_origin: 'resumed', resumed_unverified: true },
      { ...base, claude_session_id: 'cs_missing', sid_origin: 'fresh' },
      { ...base, claude_session_id: 'cs_string', sid_origin: 'fresh', resumed_unverified: 'false' },
      { ...base, claude_session_id: 'cs_zero', sid_origin: 'fresh', resumed_unverified: 0 },
      { ...base, claude_session_id: 'cs_null', sid_origin: 'fresh', resumed_unverified: null }
    ])
    await issuer.pollOnce()
    for (const cid of ['cs_true', 'cs_missing', 'cs_string', 'cs_zero', 'cs_null']) {
      expect(attestCount(h.grantsDir, cid)).toBe(0)
      expect(issuer.getLiveLaunches().has(cid)).toBe(false)
    }
    expect(issuer.isAttestationLive('default', 'hs_gate')).toBe(false)

    // A binding change can't sneak one in either.
    await issuer.onBindingChanged('default', 'hs_gate')
    await issuer.revoke('default', 'hs_gate')
    expect(attestCount(h.grantsDir, 'cs_true')).toBe(0)
    issuer.dispose()
  })

  test('a Claude sid that later shows up unverified stops being attested and refreshed', async () => {
    const launches: unknown[] = [{ ...base, claude_session_id: 'cs_flip', resumed_unverified: false }]
    const { h, issuer } = gateHarness(launches)
    await issuer.pollOnce()
    expect(attestCount(h.grantsDir, 'cs_flip')).toBe(1)

    launches[0] = { ...base, claude_session_id: 'cs_flip', launch_seq: 2, sid_origin: 'resumed', resumed_unverified: true }
    h.advanceTime(1000)
    await issuer.pollOnce()
    expect(attestCount(h.grantsDir, 'cs_flip')).toBe(1)
    expect(issuer.getLiveLaunches().has('cs_flip')).toBe(false)
    issuer.dispose()
  })
})
