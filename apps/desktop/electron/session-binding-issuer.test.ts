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
      launch_seq: 1,
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
      launch_seq: 2
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
      launch_seq: 1
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
      launch_seq: 1
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
      launch_seq: 1
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
      launch_seq: 1
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
      launch_seq: 1,
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
