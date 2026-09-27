/**
 * #60 U17 threat suite, Electron half (VERIFY addendum §4.5, §4.7, §8.2; items the UI builder left for
 * U17). Every test drives the REAL main-process modules: the key store (`owner-grant-key.ts`), the
 * anchor reader (`owner-grant-anchor.ts`), the enable/launch controller (`owner-grant-anchor-install.ts`),
 * the signing core behind the confirm handler (`owner-grant-sign.ts` via `owner-forward-confirm.ts`) and
 * the chip verifier (`owner-grant-verify.ts`). Only OS boundaries are faked: `safeStorage` (a reversible
 * mock wrap), `dialog.showMessageBox`, Touch ID, the admin runner, and the root-owned `/Library` path
 * (an in-memory fs that answers only the anchor path). Grants and key blobs live in mkdtemp dirs. No
 * window, dialog, admin prompt, Keychain or `/Library` access happens.
 *
 * Each test's comment names the MUTATION (a deliberately weakened defence) it was shown to fail against
 * before the source was restored.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  createOwnerForwardConfirmHandler,
  DIALOG_TEXT_FULL_MAX,
  isAppChromeSender,
  type OwnerForwardConfirmDeps
} from './owner-forward-confirm'
import { OWNER_ANCHOR_DIR, OWNER_ANCHOR_PATH, type OwnerAnchorFs, readTrustedOwnerAnchor } from './owner-grant-anchor'
import { createOwnerGrantController } from './owner-grant-anchor-install'
import { createOwnerKeyStore, OWNER_KEY_FILE, type SafeStorageLike } from './owner-grant-key'
import { verifyStoredOwnerGrant } from './owner-grant-verify'

const UID = process.getuid?.() ?? 501
const tmpDirs: string[] = []

function tmpDir(prefix = 'ogt-'): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  tmpDirs.push(dir)

  return dir
}

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }

  vi.restoreAllMocks()
})

/** safeStorage stand-in. In a signed build the wrap is bound to the app identity; in dev (R6) any code
 *  running the same Electron binary can wrap, which is exactly the attacker these tests model. */
const mockSafeStorage: SafeStorageLike = {
  isEncryptionAvailable: () => true,
  encryptString: (plain: string) => Buffer.from('W:' + Buffer.from(plain).toString('base64')),
  decryptString: (wrapped: Buffer) => Buffer.from(wrapped.toString().slice(2), 'base64').toString()
}

// -- the /Library boundary ---------------------------------------------------------------------

interface FakeStatSpec {
  uid: number
  mode: number
  kind: 'dir' | 'file'
  ino: number
}

function fakeStat(spec: FakeStatSpec, size = 0) {
  return {
    uid: spec.uid,
    mode: spec.mode,
    dev: 7,
    ino: spec.ino,
    size,
    isSymbolicLink: () => false,
    isDirectory: () => spec.kind === 'dir',
    isFile: () => spec.kind === 'file'
  }
}

/** Answers ONLY the hard-coded anchor path and its parents; everything else is ENOENT. */
function fakeLibraryRoot(
  data: Buffer,
  opts: { fileUid?: number; fileMode?: number; dirUid?: number; dirMode?: number } = {}
): OwnerAnchorFs {
  const chain = ['/']

  for (const part of OWNER_ANCHOR_DIR.split('/').filter(Boolean)) {
    chain.push(path.posix.join(chain[chain.length - 1], part))
  }

  const file: FakeStatSpec = { uid: opts.fileUid ?? 0, mode: opts.fileMode ?? 0o100644, kind: 'file', ino: 4242 }

  const statFor = (p: string) => {
    if (p === OWNER_ANCHOR_PATH) {
      return fakeStat(file, data.length)
    }

    if (p === OWNER_ANCHOR_DIR) {
      return fakeStat({ uid: opts.dirUid ?? 0, mode: opts.dirMode ?? 0o40755, kind: 'dir', ino: 4241 })
    }

    if (chain.includes(p)) {
      return fakeStat({ uid: 0, mode: 0o40755, kind: 'dir', ino: 4000 + chain.indexOf(p) })
    }

    throw Object.assign(new Error(`ENOENT: ${p}`), { code: 'ENOENT' })
  }

  return {
    lstatSync: statFor,
    openSync: (p: string) => {
      statFor(p)

      return 99
    },
    fstatSync: () => fakeStat(file, data.length),
    readFileSync: () => Buffer.from(data),
    closeSync: () => {},
    constants: { O_RDONLY: 0, O_NOFOLLOW: 0, O_NONBLOCK: 0 }
  }
}

function anchorJson(keys: Array<{ kid: string; pub: string; status?: string }>, grantsDir: string): Buffer {
  return Buffer.from(
    JSON.stringify({
      format: 'hermes-owner-anchor/v1',
      owner_uid: UID,
      grants_dir: grantsDir,
      keys: keys.map(k => ({
        kid: k.kid,
        alg: 'Ed25519',
        pub: k.pub,
        status: k.status ?? 'active',
        not_before: 0,
        retired_at: null
      }))
    })
  )
}

// -- a whole main process, wired the way main.ts wires it --------------------------------------

function ownerMain(opts: { keyDir?: string; anchorFs?: (grantsDir: string) => OwnerAnchorFs } = {}) {
  const base = tmpDir()
  const keyDir = opts.keyDir ?? path.join(base, 'key')
  const grantsDir = path.join(base, 'grants')
  const store = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir, uid: UID })
  const readAnchor = () => readTrustedOwnerAnchor({ fs: opts.anchorFs!(grantsDir), uid: UID })
  const adminRunner = { run: vi.fn(async () => ({ ok: false, code: 1, stdout: '', stderr: 'test' })) }
  const adminConfirm = vi.fn(async () => false)

  const controller = createOwnerGrantController({
    store,
    readAnchor,
    adminRunner: adminRunner as any,
    confirm: adminConfirm,
    ownerUid: UID,
    grantsDir,
    platform: 'darwin'
  })

  const appWindows: Array<{ webContents: unknown }> = []
  const showMessageBox = vi.fn(async (_options: any) => ({ response: 0, checkboxChecked: true }))
  const touchId = { canPrompt: vi.fn(() => true), prompt: vi.fn(async (_reason: string) => {}) }

  const deps: OwnerForwardConfirmDeps = {
    // main.ts: isAppChromeSender(event, sender => BrowserWindow.getAllWindows().some(win => win.webContents === sender))
    isTrustedSender: event => isAppChromeSender(event, sender => appWindows.some(w => w.webContents === sender)),
    backendIdForProfile: profile => (profile === 'default' ? 'spawn-live' : null),
    resolveSession: vi.fn(async (profile: string, id: string) =>
      id === 'missing' ? null : { title: `Main title ${profile}/${id}`, claude_session_id: null }
    ),
    showMessageBox,
    touchId,
    store,
    grantsDir,
    ownerUid: UID,
    now: () => Date.now(),
    formatTime: ms => `T${ms}`
  }

  const confirm = createOwnerForwardConfirmHandler(deps)
  const grantFiles = () => (fs.existsSync(grantsDir) ? fs.readdirSync(grantsDir).filter(n => n.endsWith('.json')) : [])

  return { adminConfirm, adminRunner, appWindows, confirm, controller, deps, grantFiles, grantsDir, keyDir, showMessageBox, store, touchId }
}

/** Electron-shaped objects: a BrowserWindow's webContents with its WebFrameMain tree, a `::preview`
 *  iframe (sandbox="allow-scripts") inside it, and a `<webview>` guest with its own main frame. */
function electronWindow() {
  const mainFrame: any = { parent: null, frames: [] as any[], url: 'app://hermes/index.html', routingId: 1, processId: 10 }
  mainFrame.top = mainFrame
  const previewFrame: any = { parent: mainFrame, top: mainFrame, frames: [], url: 'about:srcdoc', routingId: 2, processId: 10 }
  mainFrame.frames.push(previewFrame)
  const webContents = { id: 1, mainFrame, getType: () => 'window', isDestroyed: () => false }

  return { mainFrame, previewFrame, webContents }
}

function webviewGuest(host: ReturnType<typeof electronWindow>) {
  const guestMain: any = { parent: null, frames: [], url: 'http://localhost:5173/', routingId: 1, processId: 22 }
  guestMain.top = guestMain

  return { mainFrame: guestMain, webContents: { id: 2, mainFrame: guestMain, getType: () => 'webview', hostWebContents: host.webContents } }
}

function forwardRequest(overrides: Record<string, unknown> = {}) {
  return {
    text: 'merge the lane after CI is green',
    gesture: 'menu',
    origin: { session_id: 'mgr', message_id: null, role: 'assistant' },
    profile: 'default',
    targets: [{ profile: 'default', session_id: 'w1' }],
    scope: [],
    ...overrides
  }
}

/** The enrolled, anchored owner: key created on the enable path, anchor pinning it. */
function enrolledOwner() {
  const keyDir = path.join(tmpDir('ogt-key-'), 'key')
  const enroll = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir, uid: UID })
  const owner = enroll.ensure()

  return { keyDir, owner }
}

// ── T-3 (Electron): a user-owned file at the anchor path ───────────────────────────────────

describe('T-3: an agent-planted anchor never enables signing in main', () => {
  // MUTATION: `requireRootOwned` in owner-grant-anchor.ts made a no-op. The planted anchor then read
  // as trusted, launch reported `ready`, and the confirm handler signed (grant file written): every
  // variant below failed. Restored.
  test.each([
    ['file owned by the agent', { fileUid: UID }],
    ['file group-writable', { fileMode: 0o100664 }],
    ['directory owned by the agent', { dirUid: UID }],
    ['directory world-writable', { dirMode: 0o40777 }]
  ])('%s: untrusted, signing off, no dialog, no grant', async (_label, planted) => {
    const { keyDir, owner } = enrolledOwner()

    // The agent pins the owner's OWN current key (so a lax reader would "match"): the point is who
    // wrote the file, not what it says.
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir), planted) })

    const read = readTrustedOwnerAnchor({ fs: fakeLibraryRoot(anchorJson([owner], m.grantsDir), planted), uid: UID })
    expect(read).toMatchObject({ ok: false, reason: 'anchor_untrusted' })

    const status = m.controller.launch()
    expect(status.state).toBe('untrusted')
    expect(status.canSign).toBe(false)

    const e = electronWindow()
    m.appWindows.push({ webContents: e.webContents })
    const result = await m.confirm({ sender: e.webContents, senderFrame: e.mainFrame }, forwardRequest())

    expect(result.ok).toBe(false)
    expect(m.grantFiles()).toEqual([])
  })

  test('control: the same key under a root-owned anchor signs', async () => {
    const { keyDir, owner } = enrolledOwner()
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir)) })

    expect(m.controller.launch().state).toBe('ready')
    const e = electronWindow()
    m.appWindows.push({ webContents: e.webContents })
    const result = await m.confirm({ sender: e.webContents, senderFrame: e.mainFrame }, forwardRequest())

    expect(result.ok).toBe(true)
    expect(m.grantFiles()).toHaveLength(1)
  })
})

// ── T-4: a swapped private-key blob ──────────────────────────────────────────────────────

describe('T-4: a swapped key blob is never loaded, so main never signs with it', () => {
  function plantAttackerBlob(keyDir: string, mode: 'own_key' | 'anchored_pub_wrong_private') {
    const attackerDir = path.join(tmpDir('ogt-atk-'), 'key')
    createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: attackerDir, uid: UID }).ensure()
    const attackerDoc = JSON.parse(fs.readFileSync(path.join(attackerDir, OWNER_KEY_FILE), 'utf8'))
    const blobPath = path.join(keyDir, OWNER_KEY_FILE)
    const ownerDoc = JSON.parse(fs.readFileSync(blobPath, 'utf8'))
    const planted = mode === 'own_key' ? attackerDoc : { ...ownerDoc, wrapped: attackerDoc.wrapped }
    fs.writeFileSync(blobPath, JSON.stringify(planted), { mode: 0o600 })
  }

  // MUTATION (own_key): the `#requireAnchored(doc)` call in `loadIfEnrolled` removed. The attacker's
  // key was then adopted at launch (publicInfo() = attacker kid, no refusal) and this case failed.
  // MUTATION (anchored_pub_wrong_private): the `pub !== doc.pub` consistency check in `#adopt` removed.
  // The attacker's private key was then adopted under the owner's kid with no refusal and this case
  // failed. Both restored.
  test.each([
    ['own_key', 'anchor_mismatch'],
    ['anchored_pub_wrong_private', 'key_blob_inconsistent']
  ] as const)('%s: launch refuses (%s), the key is not adopted, confirm signs nothing', async (mode, refusal) => {
    const { keyDir, owner } = enrolledOwner()
    plantAttackerBlob(keyDir, mode)
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir)) })

    const status = m.controller.launch()
    expect(status.refusal).toBe(refusal)
    expect(status.canSign).toBe(false)
    expect(m.store.publicInfo()).toBeNull()

    const e = electronWindow()
    m.appWindows.push({ webContents: e.webContents })
    const result = await m.confirm({ sender: e.webContents, senderFrame: e.mainFrame }, forwardRequest())

    expect(result.ok).toBe(false)
    expect(m.showMessageBox).not.toHaveBeenCalled()
    expect(m.grantFiles()).toEqual([])
  })
})

// ── the sender check against Electron-shaped frames ─────────────────────────────────────

describe('sender check: only the app window main frame reaches the confirm', () => {
  // MUTATION A: isAppChromeSender weakened to "any top-level frame" (`return !!e.senderFrame &&
  // !e.senderFrame.parent`). The <webview> guest (its own main frame has no parent) then passed and
  // this test failed. MUTATION B: the frame identity clause (`e.senderFrame === sender.mainFrame &&
  // !parent`) dropped. The ::preview iframe then passed and this test failed. Both restored.
  test('::preview iframe, webview guest, stale/destroyed frames and foreign contents are refused', async () => {
    const { keyDir, owner } = enrolledOwner()
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir)) })
    m.controller.launch()
    const win = electronWindow()
    const guest = webviewGuest(win)
    m.appWindows.push({ webContents: win.webContents })
    const staleFrame = { parent: null, frames: [], url: 'app://hermes/index.html', routingId: 1, processId: 10 }

    const hostile: Array<[string, unknown]> = [
      ['::preview iframe', { sender: win.webContents, senderFrame: win.previewFrame }],
      ['<webview> guest', { sender: guest.webContents, senderFrame: guest.mainFrame }],
      ['guest lying about its type', { sender: { ...guest.webContents, getType: () => 'window' }, senderFrame: guest.mainFrame }],
      ['stale frame after navigation', { sender: win.webContents, senderFrame: staleFrame }],
      ['destroyed frame (senderFrame null)', { sender: win.webContents, senderFrame: null }],
      ['getType throws', { sender: { ...win.webContents, getType: () => { throw new Error('gone') } }, senderFrame: win.mainFrame }],
      ['no event', null]
    ]

    for (const [label, event] of hostile) {
      const result = await m.confirm(event, forwardRequest())
      expect(result, label).toMatchObject({ ok: false, code: 'untrusted_sender' })
    }

    expect(m.showMessageBox).not.toHaveBeenCalled()
    expect(m.grantFiles()).toEqual([])

    // Control: the app window's own main frame reaches the native confirm.
    expect((await m.confirm({ sender: win.webContents, senderFrame: win.mainFrame }, forwardRequest())).ok).toBe(true)
  })
})

// ── renderer compromise: every bridge function, hostile args ──────────────────────────────

describe('renderer compromise: nothing signs unless the native confirm showed exactly what is signed', () => {
  function decode(envelopePayload: string): any {
    return JSON.parse(Buffer.from(envelopePayload, 'base64url').toString('utf8'))
  }

  function throwingGetter() {
    const o: any = { gesture: 'menu', profile: 'default', origin: { session_id: 'mgr' }, targets: [{ profile: 'default', session_id: 'w1' }] }
    Object.defineProperty(o, 'text', { enumerable: true, get: () => 'benign text shown to the owner' })

    return o
  }

  // MUTATION: in owner-grant-sign.ts `confirmAndSignGrants`, the `ports.confirm(model)` result ignored
  // (treated as confirmed without calling the port). Hostile calls then signed with zero dialogs and
  // the per-call "a signature needs one dialog showing it" invariant failed. Restored.
  test('the preload bridge, called with hostile args, signs only what one native dialog displayed', async () => {
    const { keyDir, owner } = enrolledOwner()
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir)) })
    m.controller.launch()
    const win = electronWindow()
    m.appWindows.push({ webContents: win.webContents })
    const signSpy = vi.spyOn(m.store, 'signEnvelope')
    const anchorRead = readTrustedOwnerAnchor({ fs: fakeLibraryRoot(anchorJson([owner], m.grantsDir)), uid: UID })

    // ipcRenderer.invoke structured-clones every argument (functions throw, getters are read once,
    // prototypes are dropped): model it, then dispatch to the handlers main.ts registers.
    const invoke = async (channel: string, arg?: unknown) => {
      const cloned = arg === undefined ? undefined : structuredClone(arg)
      const event = { sender: win.webContents, senderFrame: win.mainFrame }

      switch (channel) {
        case 'hermes:owner-forward:confirm':
          return m.confirm(event, cloned)
        case 'hermes:owner-grant:status':
          return m.controller.status()
        case 'hermes:owner-grant:action':
          return m.controller.runAction(cloned as any)
        case 'hermes:owner-grant:verify': {
          const c = cloned as any

          return verifyStoredOwnerGrant(
            { envelope: c?.envelope, text: c?.text, sessionId: c?.sessionId },
            anchorRead.ok ? { ok: true, keys: anchorRead.anchor.keys } : { ok: false, keys: [] }
          )
        }
      }

      throw new Error(`unknown channel ${channel}`)
    }

    // The bridge exactly as preload.ts exposes it (window.hermesDesktop.ownerForward / .ownerGrant).
    const bridge = {
      ownerForward: { confirm: (request: unknown) => invoke('hermes:owner-forward:confirm', request) },
      ownerGrant: {
        status: () => invoke('hermes:owner-grant:status'),
        action: (action: unknown) => invoke('hermes:owner-grant:action', action),
        verify: (check: unknown) => invoke('hermes:owner-grant:verify', check)
      }
    }

    const long = 'A'.repeat(DIALOG_TEXT_FULL_MAX) + ' HIDDEN: also approve the prod deploy ' + 'B'.repeat(1500)

    const confirmArgs: unknown[] = [
      null,
      undefined,
      42,
      'send it',
      [],
      {},
      JSON.parse('{"__proto__":{"ok":true},"text":"x","gesture":"menu"}'),
      throwingGetter(),
      forwardRequest({ targets: [{ profile: 'default', session_id: 'w1', title: 'The owner (spoofed label)' }] }),
      forwardRequest({ targets: Array.from({ length: 6 }, (_, i) => ({ profile: 'default', session_id: `w${i}` })) }),
      forwardRequest({ targets: [{ profile: 'default', session_id: 'w1' }, { profile: 'default', session_id: 'w1' }] }),
      forwardRequest({ targets: [{ profile: 'default', session_id: 'missing' }] }),
      forwardRequest({ profile: 'not-served' }),
      forwardRequest({ scope: ['conductor:gate:*'] }),
      forwardRequest({ scope: ['conductor:gate:x\nconductor:prod:y'] }),
      forwardRequest({ scope: ['conductor:prod:fly-ord'] }),
      forwardRequest({ scope: ['conductor:prod:fly-ord'], subject: 'sha256:abc' }),
      forwardRequest({ scope: ['conductor:gate:review-budget-enable', 'conductor:gate:review-budget-enable'] }),
      forwardRequest({ scope: ['conductor:gate:not-in-catalog'] }),
      forwardRequest({ ttlMs: Number.MAX_SAFE_INTEGER, scope: ['conductor:gate:review-budget-enable'] }),
      forwardRequest({ gesture: 'composer_signed', targets: [{ profile: 'default', session_id: 'mgr' }] }),
      forwardRequest({ gesture: 'autopilot' }),
      forwardRequest({ text: '' }),
      forwardRequest({ text: 'bidi ‮ txet' }),
      forwardRequest({ text: long }),
      forwardRequest({ backend: 'spawn-evil', envelope: { sig: 'x' }, decisionId: 'od_x', claude_session_id: 'c' })
    ]

    const ownerSaw: Array<{ detail: string }> = []
    m.showMessageBox.mockImplementation(async (options: any) => {
      ownerSaw.push({ detail: String(options.detail) })

      return { response: 0, checkboxChecked: true } // the owner rubber-stamps (R3): worst case
    })

    let signedCalls = 0

    for (const arg of confirmArgs) {
      const dialogsBefore = ownerSaw.length
      const signsBefore = signSpy.mock.calls.length
      let result: any

      try {
        result = await bridge.ownerForward.confirm(arg)
      } catch {
        result = { ok: false, threw: true } // structuredClone refused it before IPC
      }

      const newSigns = signSpy.mock.calls.slice(signsBefore)

      if (newSigns.length === 0) {
        expect(result?.ok, JSON.stringify(arg)?.slice(0, 80)).not.toBe(true)
        continue
      }

      // A signature needs exactly one native dialog in this call, showing exactly what was signed.
      signedCalls += 1
      expect(ownerSaw.length - dialogsBefore).toBe(1)
      const shown = ownerSaw[ownerSaw.length - 1].detail

      for (const [payloadBytes] of newSigns) {
        const claims = JSON.parse(Buffer.from(payloadBytes as Uint8Array).toString('utf8'))

        for (const t of claims.targets) {
          expect(shown).toContain(`Main title default/${t.session_id}`)
        }

        for (const s of claims.scope) {
          expect(shown).toContain(s)
        }

        if (claims.text.length <= DIALOG_TEXT_FULL_MAX) {
          expect(shown).toContain(claims.text)
        } else {
          // Long texts: head + tail with the omitted count, never silently cut (see report, P2).
          expect(shown).toMatch(/characters omitted/)
          expect(shown).toContain(`${claims.text.length} characters`)
        }

        expect(shown).not.toContain('spoofed label')
        expect(claims.backend).toBe('spawn-live')
      }

      if (result?.ok) {
        const env = decode(result.envelope.payload)
        expect(result.targets).toEqual(env.forward_targets)
      }
    }

    expect(signedCalls).toBeGreaterThan(0) // the loop really exercised the signing path

    // Prod scope: Touch ID was demanded after Send for the prod request that signed.
    expect(m.touchId.prompt).toHaveBeenCalled()

    // The other bridge functions never sign, whatever they are given.
    const signsBefore = signSpy.mock.calls.length

    for (const action of ['enable', 'rotate', 'revoke', 'sign', '__proto__', null, { action: 'enable' }]) {
      await bridge.ownerGrant.action(action).catch(() => null)
    }

    for (const check of [null, {}, { envelope: { format: 'hermes-owner-grant/v1', kid: owner.kid, payload: '', sig: '' } }]) {
      const verdict: any = await bridge.ownerGrant.verify(check)
      expect(verdict.state).toBe('unverified')
    }

    await bridge.ownerGrant.status()
    expect(signSpy.mock.calls.length).toBe(signsBefore)
    expect(m.adminRunner.run).not.toHaveBeenCalled() // the owner declined every admin confirm
  })

  // P2 finding (strict expected-failure, see the U17 report): the addendum §2.2 says
  // `source_session.role` "is looked up by main itself, never taken from the renderer". The confirm
  // handler takes it from the renderer (`parseRequest` reads `origin.role`) and the dialog does not show
  // it, so a compromised renderer can sign role "user" (owner typed it) for a manager-drafted text.
  // When main resolves the role itself this test passes and `test.fails` flips, forcing an update.
  test.fails('source_session.role in a signed grant comes from main, not the renderer', async () => {
    const { keyDir, owner } = enrolledOwner()
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir)) })
    m.controller.launch()
    const win = electronWindow()
    m.appWindows.push({ webContents: win.webContents })

    const result: any = await m.confirm(
      { sender: win.webContents, senderFrame: win.mainFrame },
      forwardRequest({ origin: { session_id: 'mgr', message_id: 'm-assistant-draft', role: 'user' } })
    )

    expect(result.ok).toBe(true)
    // main's resolveSession (the only main-side lookup) knows nothing of message roles
    expect(decode(result.envelope.payload).source_session.role).not.toBe('user')
  })
})

// ── T-7 at the chip: lineage never widens "verified" ────────────────────────────────────

describe('T-7: the chip verifies only for a session the owner signed', () => {
  // MUTATION: the session clause in verifyStoredOwnerGrant (`targets.some(t => t.session_id ===
  // check.sessionId)`) removed. The compression tip then showed "verified" for the parent's grant and
  // this test failed. Restored.
  test('a forward signed for the parent reads unverified in the compression tip; verified in the parent', async () => {
    const { keyDir, owner } = enrolledOwner()
    const m = ownerMain({ keyDir, anchorFs: dir => fakeLibraryRoot(anchorJson([owner], dir)) })
    m.controller.launch()
    const win = electronWindow()
    m.appWindows.push({ webContents: win.webContents })
    const req = forwardRequest({ targets: [{ profile: 'default', session_id: 'worker-a' }] })
    const signed: any = await m.confirm({ sender: win.webContents, senderFrame: win.mainFrame }, req)
    expect(signed.ok).toBe(true)
    const read = readTrustedOwnerAnchor({ fs: fakeLibraryRoot(anchorJson([owner], m.grantsDir)), uid: UID })
    const anchor = read.ok ? { ok: true, keys: read.anchor.keys } : { ok: false, keys: [] }

    expect(verifyStoredOwnerGrant({ envelope: signed.envelope, text: req.text, sessionId: 'worker-a' }, anchor)).toEqual({
      state: 'verified'
    })
    expect(verifyStoredOwnerGrant({ envelope: signed.envelope, text: req.text, sessionId: 'worker-a-tip' }, anchor)).toEqual({
      state: 'unverified',
      reason: 'session_mismatch'
    })
  })
})
