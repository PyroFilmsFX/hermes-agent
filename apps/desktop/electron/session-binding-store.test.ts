import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, test } from 'vitest'

import {
  createOwnerKeyStore,
  kidForPub,
  type SafeStorageLike
} from './owner-grant-key'
import {
  createSessionBindingStore,
  type SessionBindingStorePorts
} from './session-binding-store'

const tmpDirs: string[] = []

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sbs-'))
  tmpDirs.push(dir)

  return dir
}

const mockSafeStorage: SafeStorageLike = {
  isEncryptionAvailable: () => true,
  encryptString: (plain: string) => Buffer.from('W:' + Buffer.from(plain).toString('base64')),
  decryptString: (wrapped: Buffer) => Buffer.from(wrapped.toString().slice(2), 'base64').toString()
}

function readyStore() {
  const base = tmpDir()
  const store = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
  const info = store.ensure()
  store.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })

  return { store, info, grantsDir: path.join(base, 'grants') }
}

function anchorFor(pubB64url: string, status = 'active') {
  const pub = Buffer.from(pubB64url, 'base64url')

  return { keys: [{ kid: kidForPub(pub), pub: pubB64url, status }] }
}

describe('session-binding-store: owner-signed durable session binding records', () => {
  test('1. bind writes a record that round-trips through loadAll', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const res = store.bind({
      profile: 'default',
      hermes_session_id: 's_test_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project',
      repo_remote: 'git@github.com:example/repo.git',
      project_id: 'p_123',
      carried_from: 's_parent_0'
    })

    expect('reason' in res).toBe(false)
    if ('state' in res) {
      expect(res.state).toBe('bound')
      expect(res.seq).toBe(1)
      expect(res.binding_nonce).toMatch(/^[A-Z2-7]{26}$/)
      expect(res.bound_at).toBeGreaterThan(0)
      expect(res.project_root).toBe('/abs/path/to/project')
      expect(res.repo_common_root).toBe('/abs/path/to/project')
      expect(res.repo_remote).toBe('git@github.com:example/repo.git')
      expect(res.project_id).toBe('p_123')
      expect(res.carried_from).toBe('s_parent_0')
      expect(res.envelope.format).toBe('hermes-session-binding/v1')
      expect(res.envelope.kid).toBe(s.info.kid)
    }

    const filePath = path.join(s.grantsDir, 'session-bindings', 'default', 's_test_1.json')
    expect(fs.existsSync(filePath)).toBe(true)

    // Load from fresh store instance
    const store2 = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const loaded = store2.loadAll()

    expect(loaded).toHaveLength(1)
    expect(loaded[0].state).toBe('bound')
    expect(loaded[0].profile).toBe('default')
    expect(loaded[0].hermes_session_id).toBe('s_test_1')
    expect(loaded[0].seq).toBe(1)
    expect(loaded[0].project_root).toBe('/abs/path/to/project')
    expect(loaded[0].repo_common_root).toBe('/abs/path/to/project')
    expect(loaded[0].repo_remote).toBe('git@github.com:example/repo.git')
    expect(loaded[0].project_id).toBe('p_123')
    expect(loaded[0].carried_from).toBe('s_parent_0')
    expect(loaded[0].verified).toBe(true)

    const got = store2.get('default', 's_test_1')
    expect(got).not.toBeNull()
    if (got) {
      expect(got.state).toBe('bound')
      expect(got.binding_nonce).toBe(loaded[0].binding_nonce)
    }
  })

  test('2. a tampered file loads as refused', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const res = store.bind({
      profile: 'default',
      hermes_session_id: 's_tamper_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project'
    })
    expect('reason' in res).toBe(false)

    const filePath = path.join(s.grantsDir, 'session-bindings', 'default', 's_tamper_1.json')
    const raw = JSON.parse(fs.readFileSync(filePath, 'utf8'))
    // Tamper with signature (valid 64-byte length but invalid signature)
    raw.sig = Buffer.alloc(64, 0xaa).toString('base64url')
    fs.writeFileSync(filePath, JSON.stringify(raw))

    const store2 = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const loaded = store2.loadAll()

    expect(loaded).toHaveLength(0)
    expect(loaded.refused).toHaveLength(1)
    expect(loaded.refused[0].reason).toBe('bad_signature')
    expect(store2.get('default', 's_tamper_1')).toBeNull()
  })

  test('3. a record signed by a now-retired kid loads as needs_reconfirm', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const res = store.bind({
      profile: 'default',
      hermes_session_id: 's_retire_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project'
    })
    expect('reason' in res).toBe(false)

    // Mark key as retired in anchor
    s.store.setAnchor({
      keys: [{ kid: s.info.kid, pub: s.info.pub, status: 'retired' }]
    })

    const store2 = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const loaded = store2.loadAll()

    expect(loaded).toHaveLength(1)
    expect(loaded[0].state).toBe('needs_reconfirm')
    expect(loaded[0].verified).toBe(false)
    expect(loaded[0].project_root).toBe('/abs/path/to/project')

    const got = store2.get('default', 's_retire_1')
    expect(got).not.toBeNull()
    if (got) {
      expect(got.state).toBe('needs_reconfirm')
      expect(got.verified).toBe(false)
    }
  })

  test('4. seq at/below the high-water is refused', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const res1 = store.bind({
      profile: 'default',
      hermes_session_id: 's_seq_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project'
    })
    expect('reason' in res1).toBe(false)
    if ('state' in res1) {
      expect(res1.seq).toBe(1)
    }

    // Explicit seq at high-water (1) is refused
    const resLow = store.bind({
      profile: 'default',
      hermes_session_id: 's_seq_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project',
      seq: 1
    })
    expect('reason' in resLow).toBe(true)
    if ('reason' in resLow) {
      expect(resLow.reason).toBe('seq_low')
    }

    // Explicit seq below high-water (0) is refused
    const resZero = store.bind({
      profile: 'default',
      hermes_session_id: 's_seq_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project',
      seq: 0
    })
    expect('reason' in resZero).toBe(true)
    if ('reason' in resZero) {
      expect(resZero.reason).toBe('seq_low')
    }

    // load-time high-water (b10 review: loadAll is re-runnable, so this assertion changed): a reload
    // re-reads the SAME record the store holds without tripping its own high-water...
    const reloaded = store.loadAll()
    expect(reloaded.refused.some(r => r.reason === 'seq_low')).toBe(false)
    expect(store.get('default', 's_seq_1')?.seq).toBe(1)

    // ...while a restored older file (rollback below the in-run high-water) is still refused.
    const recordPath = path.join(s.grantsDir, 'session-bindings', 'default', 's_seq_1.json')
    const seqOneFile = fs.readFileSync(recordPath, 'utf8')
    const bumped = store.bind({
      profile: 'default',
      hermes_session_id: 's_seq_1',
      project_root: '/abs/path/to/other',
      repo_common_root: '/abs/path/to/other'
    })
    expect('seq' in bumped && bumped.seq).toBe(2)
    fs.writeFileSync(recordPath, seqOneFile)
    const loaded = store.loadAll()
    expect(loaded.refused.some(r => r.reason === 'seq_low')).toBe(true)
    expect(store.get('default', 's_seq_1')?.seq).toBe(2)
    expect(store.get('default', 's_seq_1')?.project_root).toBe('/abs/path/to/other')
  })

  test('5. unbind increments seq and rotates the nonce', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const bound = store.bind({
      profile: 'default',
      hermes_session_id: 's_unbind_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project'
    })
    expect('reason' in bound).toBe(false)
    let boundNonce = ''
    if ('state' in bound) {
      expect(bound.state).toBe('bound')
      expect(bound.seq).toBe(1)
      boundNonce = bound.binding_nonce
    }

    const unbound = store.unbind('default', 's_unbind_1')
    expect('reason' in unbound).toBe(false)
    if ('state' in unbound) {
      expect(unbound.state).toBe('unbound')
      expect(unbound.seq).toBe(2)
      expect(unbound.binding_nonce).not.toBe(boundNonce)
      expect(unbound.project_root).toBeNull()
      expect(unbound.repo_common_root).toBeNull()
      expect(unbound.repo_remote).toBeNull()
      expect(unbound.project_id).toBeNull()
    }

    const got = store.get('default', 's_unbind_1')
    expect(got).not.toBeNull()
    if (got) {
      expect(got.state).toBe('unbound')
      expect(got.seq).toBe(2)
    }

    // Verify on disk file round-trips as unbound
    const store2 = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const loaded = store2.loadAll()
    expect(loaded).toHaveLength(1)
    expect(loaded[0].state).toBe('unbound')
    expect(loaded[0].seq).toBe(2)
    expect(loaded[0].project_root).toBeNull()
  })

  test('6. path components with .. or / are rejected', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const testCases: Array<{ profile: string; sid: string }> = [
      { profile: '../escape', sid: 's1' },
      { profile: 'default/sub', sid: 's1' },
      { profile: 'default\\sub', sid: 's1' },
      { profile: 'default', sid: 'sub/dir' },
      { profile: 'default', sid: 'sub\\dir' },
      { profile: 'default', sid: '..' },
      { profile: 'default', sid: '../s1' },
      { profile: 'default', sid: 's1\0evil' },
      { profile: 'def\0ault', sid: 's1' },
      { profile: '', sid: 's1' },
      { profile: 'default', sid: '' }
    ]

    for (const tc of testCases) {
      const res = store.bind({
        profile: tc.profile,
        hermes_session_id: tc.sid,
        project_root: '/abs/path',
        repo_common_root: '/abs/path'
      })
      expect('reason' in res).toBe(true)
      if ('reason' in res) {
        expect(res.reason).toBe('bad_input')
      }

      const unbindRes = store.unbind(tc.profile, tc.sid)
      expect('reason' in unbindRes).toBe(true)
      if ('reason' in unbindRes) {
        expect(unbindRes.reason).toBe('bad_input')
      }

      expect(store.get(tc.profile, tc.sid)).toBeNull()
    }
  })

  test('7. a symlinked session-bindings dir is refused', () => {
    const s = readyStore()
    const evilTarget = tmpDir()

    fs.mkdirSync(s.grantsDir, { recursive: true, mode: 0o700 })
    fs.symlinkSync(evilTarget, path.join(s.grantsDir, 'session-bindings'))

    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const res = store.bind({
      profile: 'default',
      hermes_session_id: 's_sym_1',
      project_root: '/abs/path',
      repo_common_root: '/abs/path'
    })
    expect('reason' in res).toBe(true)
    if ('reason' in res) {
      expect(res.reason).toBe('symlink_refused')
    }

    const loaded = store.loadAll()
    expect(loaded).toHaveLength(0)
    expect(loaded.refused).toHaveLength(1)
    expect(loaded.refused[0].reason).toBe('symlink_refused')
  })

  test('8. anchor-missing surfaces as signing off', () => {
    const s = readyStore()
    s.store.setAnchor(null)

    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const res = store.bind({
      profile: 'default',
      hermes_session_id: 's_no_anchor_1',
      project_root: '/abs/path',
      repo_common_root: '/abs/path'
    })
    expect('reason' in res).toBe(true)
    if ('reason' in res) {
      expect(res.reason).toBe('signing_off')
    }

    const unbindRes = store.unbind('default', 's_no_anchor_1')
    expect('reason' in unbindRes).toBe(true)
    if ('reason' in unbindRes) {
      expect(unbindRes.reason).toBe('signing_off')
    }
  })

  test('9. resignAll after a key rotation makes records verify under the new active kid', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const bound = store.bind({
      profile: 'default',
      hermes_session_id: 's_rot_1',
      project_root: '/abs/path/to/project',
      repo_common_root: '/abs/path/to/project'
    })
    expect('reason' in bound).toBe(false)
    if ('state' in bound) {
      expect(bound.envelope.kid).toBe(s.info.kid)
    }

    // Key rotation: new fresh key pinned as active, old key retired
    const fresh = s.store.beginFreshKey()
    s.store.stagePendingKey()
    s.store.setAnchor({
      keys: [
        { kid: s.info.kid, pub: s.info.pub, status: 'retired' },
        { kid: fresh.kid, pub: fresh.pub, status: 'active' }
      ]
    })
    s.store.commitPendingKey()

    expect(s.store.publicInfo()?.kid).toBe(fresh.kid)

    // Re-sign all in-memory verified records with the new active key
    const resigned = store.resignAll()
    expect(resigned.resigned).toBe(1)
    expect(store.get('default', 's_rot_1')?.envelope.kid).toBe(fresh.kid)

    // A fresh store reading from disk now verifies under the new active kid
    const store2 = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const loaded = store2.loadAll()

    expect(loaded).toHaveLength(1)
    expect(loaded[0].envelope.kid).toBe(fresh.kid)
    expect(loaded[0].state).toBe('bound')
    expect(loaded[0].verified).toBe(true)
    expect(loaded.refused).toHaveLength(0)
  })

  test('multi-profile isolation: same hermes_session_id in two profiles stored and loaded separately', () => {
    const s = readyStore()
    const store = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })

    const r1 = store.bind({
      profile: 'profile_a',
      hermes_session_id: 'session_common',
      project_root: '/abs/a',
      repo_common_root: '/abs/a'
    })
    expect('reason' in r1).toBe(false)

    const r2 = store.bind({
      profile: 'profile_b',
      hermes_session_id: 'session_common',
      project_root: '/abs/b',
      repo_common_root: '/abs/b'
    })
    expect('reason' in r2).toBe(false)

    expect(store.get('profile_a', 'session_common')?.project_root).toBe('/abs/a')
    expect(store.get('profile_b', 'session_common')?.project_root).toBe('/abs/b')

    const store2 = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const loaded = store2.loadAll()
    expect(loaded).toHaveLength(2)
  })
})

describe('session-binding-store: b10 review — bindings load after the key', () => {
  test('a load before the key/anchor surfaces needs_reconfirm; a re-run after them verifies the same records', () => {
    const s = readyStore()
    const writer = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const bound = writer.bind({
      profile: 'default',
      hermes_session_id: 's_restart',
      project_root: '/abs/path/restart',
      repo_common_root: '/abs/path/restart'
    })
    expect('state' in bound && bound.state).toBe('bound')

    // A fresh process before loadOwnerGrantKeyAtLaunch(): no anchor yet.
    const anchor = { keys: [{ kid: s.info.kid, pub: s.info.pub, status: 'active' }] }
    s.store.setAnchor(null)
    const restarted = createSessionBindingStore({ store: s.store, grantsDir: s.grantsDir })
    const early = restarted.loadAll()
    expect(early).toHaveLength(1)
    expect(restarted.get('default', 's_restart')?.state).toBe('needs_reconfirm')

    // After the key + anchor load, a re-run of loadAll must not trip its own high-water.
    s.store.setAnchor(anchor)
    const late = restarted.loadAll()
    expect(late.refused).toEqual([])
    const got = restarted.get('default', 's_restart')
    expect(got?.state).toBe('bound')
    expect(got?.verified).toBe(true)
    expect(got?.seq).toBe(1)

    // And the in-run high-water still holds: the next write is seq 2.
    const next = restarted.unbind('default', 's_restart')
    expect('seq' in next && next.seq).toBe(2)
  })

  test('main.ts loads bindings and starts the issuer only after the owner key (source pin)', () => {
    const src = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')
    const loadCalls = src.match(/sessionBindingStore\.loadAll\(\)/g) ?? []
    const startCalls = src.match(/sessionBindingIssuer\.start\(\)/g) ?? []
    expect(loadCalls).toHaveLength(1)
    expect(startCalls).toHaveLength(1)

    const helperAt = src.indexOf('function startSessionBindingsAfterOwnerKey(')
    expect(helperAt).toBeGreaterThan(-1)
    const helperBody = src.slice(helperAt, src.indexOf('\n}\n', helperAt))
    expect(helperBody).toContain('sessionBindingStore.loadAll()')
    expect(helperBody).toContain('sessionBindingIssuer.start()')
    expect(helperBody.indexOf('loadAll()')).toBeLessThan(helperBody.indexOf('start()'))

    const launchAt = src.indexOf('function loadOwnerGrantKeyAtLaunch(')
    const launchBody = src.slice(launchAt, src.indexOf('\n}\n', launchAt))
    expect(launchBody.indexOf('ownerGrantController.launch()')).toBeGreaterThan(-1)
    expect(launchBody.indexOf('startSessionBindingsAfterOwnerKey()')).toBeGreaterThan(
      launchBody.indexOf('ownerGrantController.launch()')
    )
  })
})
