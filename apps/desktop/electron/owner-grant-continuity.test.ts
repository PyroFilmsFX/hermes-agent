/**
 * b9 §3: the relaunch continuity issuer (Electron main only) and the proof that no IPC channel,
 * gateway method or tool can mint `conductor:continuity:session-relaunch`.
 *
 * safeStorage is a mock; grants dirs are mkdtemp dirs under os.tmpdir(), never ~/.hermes.
 */

import { createHash, createPublicKey, verify as edVerify } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  collectLiveSessions,
  CONTINUITY_OBSERVATION_MAX_AGE_MS,
  CONTINUITY_POLL_MS,
  CONTINUITY_SCOPE,
  CONTINUITY_TTL_MS,
  type ContinuityIssuerPorts,
  type ContinuityLiveSession,
  type ContinuitySignPorts,
  createOwnerGrantContinuityIssuer,
  issueContinuityGrant
} from './owner-grant-continuity'
import { createOwnerKeyStore, GRANT_DOMAIN_PREFIX, type SafeStorageLike } from './owner-grant-key'
import { confirmAndSignGrants, GESTURES, type SignPorts, type SignRequest } from './owner-grant-sign'

const ELECTRON_DIR = __dirname
const REPO = path.resolve(__dirname, '../../..')
const NOW = 1_790_000_000_000
const OLD = '5b0d8a3e-0c7e-4c2e-9d5f-3f1f7d9a0b11'
const NEW = '9c4e2f10-7a1b-4d3c-8e5f-0a1b2c3d4e5f'

const tmpDirs: string[] = []

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ogc-'))
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

function signPorts(over: Partial<ContinuitySignPorts> = {}) {
  const s = readyStore()

  const ports: ContinuitySignPorts = {
    store: s.store,
    grantsDir: s.grantsDir,
    ownerUid: 501,
    now: () => NOW,
    ...over
  }

  return { ...s, ports }
}

const relaunch = (over: Record<string, unknown> = {}) => ({
  backendProfile: 'default',
  profile: 'default',
  hermesSessionId: 'sess-1',
  backend: 'spawn_new',
  oldClaudeSid: OLD,
  newClaudeSid: NEW,
  observed: { backend: 'spawn_old', claudeSid: OLD, at: NOW - 30_000 },
  ...over
})

function grants(dir: string): string[] {
  return fs.existsSync(dir) ? fs.readdirSync(dir).filter(n => n.endsWith('.json')) : []
}

describe('issueContinuityGrant: signs only when old is main\'s own pre-relaunch observation', () => {
  test('the grant: subject old:new, hermes_session_id + at, 15 min, single use, bound to the NEW Claude session', async () => {
    const h = signPorts()

    const out = await issueContinuityGrant(relaunch(), h.ports)

    expect(out.issued).toBe(true)

    if (!out.issued) {
      return
    }

    const bytes = Buffer.from(out.envelope.payload, 'base64url')
    const p = JSON.parse(bytes.toString('utf8'))
    expect(p.scope).toEqual([CONTINUITY_SCOPE])
    expect(p.single_use).toEqual([CONTINUITY_SCOPE])
    expect(p.subject).toEqual({ [CONTINUITY_SCOPE]: `${OLD}:${NEW}` })
    expect(p.hermes_session_id).toBe('sess-1')
    expect(p.at).toBe(NOW)
    expect(p.expires_at - p.issued_at).toBe(15 * 60_000)
    expect(CONTINUITY_TTL_MS).toBe(15 * 60_000)
    expect(p.aud).toEqual(['hermes-owner-verify'])
    expect(p.targets).toEqual([{ session_id: 'sess-1', claude_session_id: NEW }])
    expect(p.backend).toBe('spawn_new')
    expect(p.text_sha256).toBe(createHash('sha256').update(p.text, 'utf8').digest('hex'))
    expect(p.text_len).toBe(Buffer.byteLength(p.text, 'utf8'))

    const pub = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: h.info.pub }, format: 'jwk' })
    expect(edVerify(null, Buffer.concat([GRANT_DOMAIN_PREFIX, bytes]), pub, Buffer.from(out.envelope.sig, 'base64url'))).toBe(true)
    expect(grants(h.grantsDir)).toEqual([path.basename(out.path)])
    expect(fs.statSync(out.path).mode & 0o777).toBe(0o600)
  })

  test('P1-1: no observation, a different observed id, or an observation by the SAME backend: nothing is signed', async () => {
    for (const [over, reason] of [
      [{ observed: null }, 'no_observation'],
      [{ observed: { backend: 'spawn_old', claudeSid: 'planted-by-agent', at: NOW } }, 'not_observed'],
      [{ observed: { backend: 'spawn_new', claudeSid: OLD, at: NOW } }, 'not_observed']
    ] as const) {
      const h = signPorts()
      expect(await issueContinuityGrant(relaunch(over), h.ports)).toEqual({ issued: false, reason })
      expect(grants(h.grantsDir)).toEqual([])
    }
  })

  test('a subject outside the grammar (old == new, a colon, blanks) is refused', async () => {
    for (const over of [{ newClaudeSid: OLD }, { oldClaudeSid: 'a:b' }, { newClaudeSid: '' }, { hermesSessionId: '' }, { backend: '' }]) {
      const h = signPorts()
      expect(await issueContinuityGrant(relaunch(over), h.ports)).toEqual({ issued: false, reason: 'bad_input' })
      expect(grants(h.grantsDir)).toEqual([])
    }
  })

  test('signing off (the anchor gate refuses): nothing is written', async () => {
    const h = signPorts()
    h.store.setAnchor(null)
    expect(await issueContinuityGrant(relaunch(), h.ports)).toEqual({ issued: false, reason: 'signing_off' })
    expect(grants(h.grantsDir)).toEqual([])
  })
})

describe('the observer and the relaunch watcher (main\'s backend lifecycle only)', () => {
  function watcher(opts: { list?: ContinuityLiveSession[]; live?: Record<string, string | null> } = {}) {
    const s = readyStore()
    let now = NOW
    const liveReads: string[] = []
    let list = opts.list ?? []
    const live = { ...(opts.live ?? {}) }

    const ports: ContinuityIssuerPorts = {
      store: s.store,
      grantsDir: s.grantsDir,
      ownerUid: 501,
      now: () => now,
      listLiveSessions: async () => list,
      readLive: async (_backendProfile, profile, sid) => {
        liveReads.push(`${profile}:${sid}`)

        return live[`${profile}:${sid}`] ?? null
      },
      // The poll interval elapses at once; the clock moves with it.
      setTimer: (fn, ms) => {
        now += ms
        queueMicrotask(fn)

        return 0
      }
    }

    return {
      ...s,
      issuer: createOwnerGrantContinuityIssuer(ports),
      liveReads,
      live,
      setList: (next: ContinuityLiveSession[]) => void (list = next),
      advance: (ms: number) => void (now += ms)
    }
  }

  test('observed before the relaunch, changed after it: exactly one grant; unchanged sessions get none', async () => {
    const w = watcher({
      list: [
        { profile: 'default', sessionId: 'moved', live: OLD },
        { profile: 'default', sessionId: 'same', live: 'same-sid' }
      ],
      live: { 'default:moved': NEW, 'default:same': 'same-sid' }
    })

    await w.issuer.observe('default', 'spawn_1')

    const first = await w.issuer.onBackendRelaunch('default', 'spawn_2')
    expect(first).toHaveLength(1)
    expect(first[0]).toMatchObject({ issued: true })
    const p = JSON.parse(Buffer.from((first[0] as any).envelope.payload, 'base64url').toString('utf8'))
    expect(p.subject[CONTINUITY_SCOPE]).toBe(`${OLD}:${NEW}`)
    // The previous backend's observations were consumed: a second relaunch signs nothing.
    expect(await w.issuer.onBackendRelaunch('default', 'spawn_3')).toEqual([])
    expect(grants(w.grantsDir)).toHaveLength(1)
  })

  test('P1-1: no pre-relaunch observation means no grant, whatever the relaunched backend reports', async () => {
    const w = watcher({ live: { 'default:s': NEW } })
    expect(await w.issuer.onBackendRelaunch('default', 'spawn_2')).toEqual([])
    expect(w.liveReads).toEqual([])
    expect(grants(w.grantsDir)).toEqual([])
  })

  test('a stale observation (older than the max age at relaunch) does not count', async () => {
    const w = watcher({ list: [{ profile: 'default', sessionId: 's', live: OLD }], live: { 'default:s': NEW } })
    await w.issuer.observe('default', 'spawn_1')
    w.advance(CONTINUITY_OBSERVATION_MAX_AGE_MS + 1)
    expect(await w.issuer.onBackendRelaunch('default', 'spawn_2')).toEqual([])
    expect(grants(w.grantsDir)).toEqual([])
  })

  test('observations of the NEW backend never become an "old" id, and a stale spawn id observes nothing', async () => {
    const w = watcher({ list: [{ profile: 'default', sessionId: 's', live: OLD }], live: { 'default:s': NEW } })
    await w.issuer.observe('default', 'spawn_1')
    const relaunched = w.issuer.onBackendRelaunch('default', 'spawn_2')
    await relaunched
    // A late observe for the dead backend is dropped.
    w.setList([{ profile: 'default', sessionId: 's', live: 'late' }])
    await w.issuer.observe('default', 'spawn_1')
    expect(await w.issuer.onBackendRelaunch('default', 'spawn_3')).toEqual([])
  })

  test('the watch waits for the new CLI, then gives up after the window', async () => {
    const w = watcher({ list: [{ profile: 'default', sessionId: 's', live: OLD }] })
    await w.issuer.observe('default', 'spawn_1')
    expect(await w.issuer.onBackendRelaunch('default', 'spawn_2')).toEqual([])
    expect(w.liveReads.length).toBeGreaterThan(1)
    expect(w.liveReads.length).toBeLessThanOrEqual(10 * 60_000 / CONTINUITY_POLL_MS)
    expect(grants(w.grantsDir)).toEqual([])
  })

  test('observes live sessions beyond the old 10-active / 20-row slice with bounded concurrency and pagination', async () => {
    // 25 total sessions:
    // Page 0 (rows 0-19): 8 ended sessions and 12 active sessions (sess-0 .. sess-11).
    // Page 1 (rows 20-24): 5 active sessions (sess-12 .. sess-16) - beyond the 20-row limit!
    // Active session 15 (sess-15) is both past the 10-active slice and on page 2 (past 20 rows).
    const rowsPage0 = [
      ...Array.from({ length: 8 }, (_, i) => ({ id: `ended-${i}`, ended_at: NOW - 100_000 })),
      ...Array.from({ length: 12 }, (_, i) => ({ id: `sess-${i}`, ended_at: null }))
    ]
    const rowsPage1 = Array.from({ length: 5 }, (_, i) => ({ id: `sess-${i + 12}`, ended_at: null }))

    const requestedPaths: string[] = []
    let inFlightReads = 0
    let maxInFlightReads = 0

    const OLD_15 = '5b0d8a3e-0c7e-4c2e-9d5f-3f1f7d9a0b15'
    const NEW_15 = '9c4e2f10-7a1b-4d3c-8e5f-0a1b2c3d4e15'

    const fetchJson = async (path: string) => {
      requestedPaths.push(path)
      if (path.includes('offset=0')) {
        return { sessions: rowsPage0, total: 25 }
      }
      if (path.includes('offset=20')) {
        return { sessions: rowsPage1, total: 25 }
      }
      return { sessions: [], total: 25 }
    }

    const readLive = async (_bp: string, _p: string, sid: string) => {
      inFlightReads++
      maxInFlightReads = Math.max(maxInFlightReads, inFlightReads)
      await new Promise(r => setTimeout(r, 5))
      inFlightReads--
      if (sid === 'sess-15') {
        return OLD_15
      }
      return null
    }

    const collected = await collectLiveSessions('default', { fetchJson, readLive }, { pageSize: 20, concurrency: 4 })

    expect(requestedPaths.some(p => p.includes('offset=0'))).toBe(true)
    expect(requestedPaths.some(p => p.includes('offset=20'))).toBe(true)
    expect(maxInFlightReads).toBeGreaterThan(0)
    expect(maxInFlightReads).toBeLessThanOrEqual(4)

    const sess15 = collected.find(s => s.sessionId === 'sess-15')
    expect(sess15).toBeDefined()
    expect(sess15?.live).toBe(OLD_15)

    const s = readyStore()
    let now = NOW
    const liveMap: Record<string, string | null> = { 'default:sess-15': NEW_15 }

    const issuer = createOwnerGrantContinuityIssuer({
      store: s.store,
      grantsDir: s.grantsDir,
      ownerUid: 501,
      now: () => now,
      listLiveSessions: backendProfile =>
        collectLiveSessions(backendProfile, { fetchJson, readLive }, { pageSize: 20, concurrency: 4 }),
      readLive: async (_bp, p, sid) => liveMap[`${p}:${sid}`] ?? null,
      setTimer: (fn, ms) => {
        now += ms
        queueMicrotask(fn)
        return 0
      }
    })

    await issuer.observe('default', 'spawn_1')

    const outcomes = await issuer.onBackendRelaunch('default', 'spawn_2')
    expect(outcomes).toHaveLength(1)
    expect(outcomes[0]).toMatchObject({ issued: true })

    const p = JSON.parse(Buffer.from((outcomes[0] as any).envelope.payload, 'base64url').toString('utf8'))
    expect(p.subject[CONTINUITY_SCOPE]).toBe(`${OLD_15}:${NEW_15}`)
    expect(p.hermes_session_id).toBe('sess-15')
    expect(grants(s.grantsDir)).toHaveLength(1)
  })

  test('the issuer has no request-facing entry point: only observe and onBackendRelaunch', () => {
    const w = watcher()
    expect(Object.keys(w.issuer).sort()).toEqual(['observe', 'onBackendRelaunch'])
  })
})

// -- non-requestability ------------------------------------------------------------------------

function sourceFiles(dir: string): string[] {
  return fs
    .readdirSync(dir, { withFileTypes: true })
    .flatMap(entry => {
      const full = path.join(dir, entry.name)

      if (entry.isDirectory()) {
        return entry.name === 'node_modules' ? [] : sourceFiles(full)
      }

      return /\.(ts|tsx|mts|cts)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : []
    })
}

/** The text of the call starting at `start` (the index of its opening paren), balanced on (). */
function callText(src: string, open: number): string {
  let depth = 0

  for (let i = open; i < src.length; i++) {
    if (src[i] === '(') {
      depth++
    } else if (src[i] === ')' && --depth === 0) {
      return src.slice(open, i + 1)
    }
  }

  return src.slice(open)
}

function functionBody(src: string, name: string): string {
  const start = src.indexOf(`function ${name}(`)
  expect(start, name).toBeGreaterThanOrEqual(0)
  let depth = 0

  for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') {
      depth++
    } else if (src[i] === '}' && --depth === 0) {
      return src.slice(start, i + 1)
    }
  }

  return src.slice(start)
}

const MINTING = /issueContinuityGrant|onBackendRelaunch|\.observe\(|ownerGrantContinuity|createOwnerGrantContinuityIssuer|continuity:session-relaunch/

describe('no IPC channel, gateway method or tool can mint the continuity grant', () => {
  test('the signing plan refuses a continuity scope on every requestable gesture, before any confirm', async () => {
    const s = readyStore()

    for (const gesture of GESTURES) {
      const confirm = vi.fn(async () => ({ confirmed: true }))
      const self = gesture === 'composer_signed'

      const req: SignRequest = {
        text: 'rebind my marker',
        gesture,
        sourceSession: { session_id: 'mgr', message_id: null, role: 'user' },
        targets: [{ profile: 'default', session_id: self ? 'mgr' : 'w1', claude_session_id: NEW, backend: 'bk' }],
        scope: [CONTINUITY_SCOPE],
        subject: { [CONTINUITY_SCOPE]: `${OLD}:${NEW}` }
      }

      const ports: SignPorts = { store: s.store, grantsDir: s.grantsDir, now: () => NOW, ownerUid: 501, confirm }

      await expect(confirmAndSignGrants(req, ports), gesture).rejects.toMatchObject({ code: 'bad_scope' })
      expect(confirm).not.toHaveBeenCalled()
    }

    expect(grants(s.grantsDir)).toEqual([])
  })

  test('only main.ts imports the issuer, and main mints only from the backend spawn hook', () => {
    const importers = sourceFiles(ELECTRON_DIR)
      .filter(file => /from '\.\/owner-grant-continuity'/.test(fs.readFileSync(file, 'utf8')))
      .map(file => path.relative(ELECTRON_DIR, file))

    expect(importers).toEqual(['main.ts'])

    const main = fs.readFileSync(path.join(ELECTRON_DIR, 'main.ts'), 'utf8')
    expect(main).not.toMatch(/issueContinuityGrant/)
    expect(main.match(/\.onBackendRelaunch\(/g) ?? []).toHaveLength(1)
    expect(functionBody(main, 'ownerGrantBackendSpawnEnv')).toMatch(/ownerGrantContinuity\.onBackendRelaunch\(/)
    // Observation is fed only by main's own timer over app-spawned backends.
    expect(main.match(/ownerGrantContinuity\.observe\(/g) ?? []).toHaveLength(1)
    expect(main).toMatch(/setInterval\(\(\) => \{\s*if \(ownerGrantKeyStore\.anchorState\(\) !== 'match'\) \{\s*return\s*\}\s*for \(const \[profile, backendId\] of ownerGrantBackendIds\) \{\s*void ownerGrantContinuity\.observe\(profile, backendId\)/)
    // P1-2: the owner-forward confirm handler's deps (its resolveSession included) never touch it.
    const confirmDeps = callText(main, main.indexOf('createOwnerForwardConfirmHandler(') + 'createOwnerForwardConfirmHandler'.length)
    expect(confirmDeps.length).toBeGreaterThan(500)
    expect(confirmDeps).not.toMatch(MINTING)
  })

  test('no registered ipcMain handler (main.ts or any *-ipc module) references the issuer or the scope', () => {
    let handlers = 0

    for (const file of sourceFiles(ELECTRON_DIR)) {
      const src = fs.readFileSync(file, 'utf8')
      const re = /ipcMain\.(?:handle|handleOnce|on|once)\(/g
      let match: RegExpExecArray | null

      while ((match = re.exec(src))) {
        const text = callText(src, match.index + match[0].length - 1)
        handlers++
        expect(text, `${path.relative(ELECTRON_DIR, file)} ${text.slice(0, 80)}`).not.toMatch(MINTING)
      }
    }

    // Sanity: the scan really enumerated the app's handlers.
    expect(handlers).toBeGreaterThan(50)
  })

  test('the preload bridges expose no continuity channel', () => {
    for (const name of fs.readdirSync(ELECTRON_DIR).filter(n => /preload.*\.ts$/.test(n) && !n.endsWith('.test.ts'))) {
      expect(fs.readFileSync(path.join(ELECTRON_DIR, name), 'utf8'), name).not.toMatch(/continuity/i)
    }
  })

  test('only the signing core and the issuer call signEnvelope (the key never leaves main)', () => {
    const callers = sourceFiles(ELECTRON_DIR)
      .filter(file => /\.signEnvelope\(/.test(fs.readFileSync(file, 'utf8')))
      .map(file => path.relative(ELECTRON_DIR, file))
      .sort()

    const allowed = ['owner-grant-continuity.ts', 'owner-grant-key.ts', 'owner-grant-sign.ts']
    expect(callers.filter(file => !allowed.includes(file))).toEqual([])
    expect(callers).toContain('owner-grant-continuity.ts')
    expect(callers).toContain('owner-grant-sign.ts')
  })

  test('the gateway and the agent tools never name the continuity scope (they hold no key to sign it)', () => {
    const hits: string[] = []

    const scan = (dir: string) => {
      if (!fs.existsSync(dir)) {
        return
      }

      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name)

        if (entry.isDirectory()) {
          if (entry.name !== '__pycache__' && entry.name !== 'node_modules') {
            scan(full)
          }
        } else if (entry.name.endsWith('.py') && /continuity:session-relaunch/.test(fs.readFileSync(full, 'utf8'))) {
          hits.push(path.relative(REPO, full))
        }
      }
    }

    for (const dir of ['tui_gateway', 'tools', 'hermes_cli', 'gateway']) {
      scan(path.join(REPO, dir))
    }

    expect(hits).toEqual([])
  })
})
