/**
 * #60 owner-forward UI path, main-process half: the native confirm adapter, the rate gates, Touch ID
 * for prod, target titles resolved by main, and the app-chrome sender check (E-1, E-2, E-7; U12).
 *
 * No real dialog, no real Touch ID, no Keychain: `showMessageBox` / `touchId` are fakes, safeStorage
 * is a mock, and grants dirs are mkdtemp dirs under os.tmpdir().
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  buildConfirmDialog,
  CANCEL_COOLDOWN_MS,
  createConfirmRateGate,
  createOwnerForwardConfirmHandler,
  DIALOG_TEXT_FULL_MAX,
  isAppChromeSender,
  MAX_CONFIRMS_PER_MINUTE,
  type OwnerForwardConfirmDeps
} from './owner-forward-confirm'
import { createOwnerKeyStore, type SafeStorageLike } from './owner-grant-key'

const tmpDirs: string[] = []

function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ofc-'))
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

function mainFrame() {
  return { parent: null, url: 'app://hermes/index.html' }
}

function chromeEvent() {
  const frame = mainFrame()
  const sender = { mainFrame: frame, getType: () => 'window' }

  return { sender, senderFrame: frame }
}

function harness(overrides: Partial<OwnerForwardConfirmDeps> = {}) {
  const base = tmpDir()
  const store = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
  const info = store.ensure()
  store.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })
  const grantsDir = path.join(base, 'grants')
  let now = NOW
  const appSenders = new Set<unknown>()

  const showMessageBox = vi.fn(async (_options: any) => ({ response: 0, checkboxChecked: false }))

  const resolveSession = vi.fn(async (profile: string, id: string) =>
    id === 'missing' ? null : { title: `Main title for ${profile}/${id}`, claude_session_id: null }
  )

  const touchId = { canPrompt: vi.fn(() => false), prompt: vi.fn(async () => {}) }

  const deps: OwnerForwardConfirmDeps = {
    isTrustedSender: event => isAppChromeSender(event, sender => appSenders.has(sender)),
    backendIdForProfile: profile => (profile === 'default' ? 'spawn_abc' : null),
    resolveSession,
    showMessageBox,
    touchId,
    store,
    grantsDir,
    ownerUid: 501,
    now: () => now,
    formatTime: ms => `T${ms}`,
    ...overrides
  }

  const handler = createOwnerForwardConfirmHandler(deps)

  const event = () => {
    const e = chromeEvent()
    appSenders.add(e.sender)

    return e
  }

  return {
    deps,
    event,
    grantsDir,
    handler,
    resolveSession,
    setNow: (ms: number) => void (now = ms),
    showMessageBox,
    touchId
  }
}

function req(overrides: Record<string, unknown> = {}) {
  return {
    text: 'merge the lane after CI is green',
    gesture: 'menu',
    origin: { session_id: 'mgr', message_id: null, role: 'assistant' },
    profile: 'default',
    targets: [{ profile: 'default', session_id: 'w1', title: 'RENDERER LABEL (ignored)' }],
    scope: [],
    ...overrides
  }
}

function grantFiles(dir: string): string[] {
  return fs.existsSync(dir) ? fs.readdirSync(dir).filter(name => name.endsWith('.json')) : []
}

describe('senderFrame: only the app main frame (chrome) may ask for a confirm', () => {
  test('a subframe (widget / ::preview iframe) is refused before any dialog', async () => {
    const h = harness()
    const e = h.event()
    const iframe = { parent: e.senderFrame, url: 'about:srcdoc' }

    const result = await h.handler({ ...e, senderFrame: iframe }, req())

    expect(result).toMatchObject({ ok: false, code: 'untrusted_sender' })
    expect(h.showMessageBox).not.toHaveBeenCalled()
    expect(grantFiles(h.grantsDir)).toEqual([])
  })

  test('a webview guest or a foreign webContents is refused', async () => {
    const h = harness()
    const e = h.event()
    const guest = { ...e.sender, getType: () => 'webview' }

    expect(await h.handler({ sender: guest, senderFrame: e.senderFrame }, req())).toMatchObject({
      ok: false,
      code: 'untrusted_sender'
    })

    const foreign = chromeEvent() // never registered as an app window
    expect(await h.handler(foreign, req())).toMatchObject({ ok: false, code: 'untrusted_sender' })
    expect(isAppChromeSender(null, () => true)).toBe(false)
    expect(h.showMessageBox).not.toHaveBeenCalled()
  })

  test('the main frame of an app window is accepted', async () => {
    const h = harness()
    const result = await h.handler(h.event(), req())

    expect(result).toMatchObject({ ok: true, targets: ['default:w1'] })
    expect(h.showMessageBox).toHaveBeenCalledTimes(1)
    expect(grantFiles(h.grantsDir)).toHaveLength(1)
  })
})

describe('the native confirm shows what main resolved, never renderer labels', () => {
  test('titles come from main (resolveSession), the renderer label is ignored', async () => {
    const h = harness()
    await h.handler(h.event(), req())

    expect(h.resolveSession).toHaveBeenCalledWith('default', 'w1', 'default')
    const options = h.showMessageBox.mock.calls[0][0]
    expect(options.detail).toContain('Main title for default/w1')
    expect(options.detail).not.toContain('RENDERER LABEL')
    expect(options.message).toBe('Send as you?')
    expect(options.buttons).toEqual(['Send', 'Cancel'])
    expect(options.cancelId).toBe(1)
  })

  test('an unknown target is refused before the dialog', async () => {
    const h = harness()
    const result = await h.handler(h.event(), req({ targets: [{ profile: 'default', session_id: 'missing' }] }))

    expect(result).toMatchObject({ ok: false, code: 'target_missing' })
    expect(h.showMessageBox).not.toHaveBeenCalled()
  })

  test('a profile with no signing backend is refused before the dialog', async () => {
    const h = harness()
    const result = await h.handler(h.event(), req({ profile: 'remote-only' }))

    expect(result).toMatchObject({ ok: false, code: 'no_backend' })
    expect(h.showMessageBox).not.toHaveBeenCalled()
  })

  test('every scope is listed with its class and expiry; prod gets a warning line', async () => {
    const h = harness()

    await h.handler(
      h.event(),
      req({
        scope: ['conductor:gate:review-budget-enable', 'conductor:prod:target'],
        subject: 'sha256:abc'
      })
    )

    const detail: string = h.showMessageBox.mock.calls[0][0].detail
    expect(detail).toContain('Enable review budget')
    expect(detail).toContain('conductor:gate:review-budget-enable')
    expect(detail).toContain('gate')
    expect(detail).toContain('conductor:prod:target')
    expect(detail).toContain('prod')
    expect(detail).toContain('sha256:abc')
    expect(detail).toContain(`T${NOW + 900_000}`)
    expect(detail).toMatch(/Production/)
  })

  test('long text shows head and tail with counts; short text is shown whole', () => {
    const long = 'A'.repeat(2000) + 'MIDDLE' + 'Z'.repeat(2000)
    const model = {
      targets: [],
      scopes: [],
      quoteOnly: true,
      expiresAt: NOW,
      text: long,
      textChars: long.length,
      textBytes: long.length,
      requiresTouchId: false,
      requiresUnrecognizedAck: false
    } as any

    const detail = buildConfirmDialog(model, ms => String(ms)).detail
    expect(long.length).toBeGreaterThan(DIALOG_TEXT_FULL_MAX)
    expect(detail).not.toContain('MIDDLE')
    expect(detail).toContain(`${long.length} characters`)
    expect(detail).toMatch(/omitted/)

    const short = buildConfirmDialog({ ...model, text: 'hello there', textChars: 11 }, ms => String(ms)).detail
    expect(short).toContain('hello there')
  })

  test('an unrecognized scope needs the dialog checkbox', async () => {
    const h = harness()
    const result = await h.handler(h.event(), req({ scope: ['conductor:gate:not-in-catalog'] }))

    expect(h.showMessageBox.mock.calls[0][0].checkboxLabel).toBeTruthy()
    expect(result).toMatchObject({ ok: false, cancelled: true, reason: 'unrecognized_scope' })
    expect(grantFiles(h.grantsDir)).toEqual([])
  })
})

describe('dialog rate gates', () => {
  test('one open dialog at a time', async () => {
    let release!: (value: { response: number }) => void
    const pending = new Promise<{ response: number }>(resolve => (release = resolve))
    const h = harness({ showMessageBox: vi.fn(() => pending) })

    const first = h.handler(h.event(), req())
    await vi.waitFor(() => expect(h.deps.showMessageBox).toHaveBeenCalledTimes(1))
    const second = await h.handler(h.event(), req())

    expect(second).toMatchObject({ ok: false, code: 'dialog_open' })
    release({ response: 0 })
    expect(await first).toMatchObject({ ok: true })
  })

  test('a 3 s cooldown after Cancel', async () => {
    const h = harness()
    h.showMessageBox.mockResolvedValueOnce({ response: 1, checkboxChecked: false })

    expect(await h.handler(h.event(), req())).toMatchObject({ ok: false, cancelled: true, reason: 'dialog' })
    expect(grantFiles(h.grantsDir)).toEqual([])

    h.setNow(NOW + CANCEL_COOLDOWN_MS - 1)
    expect(await h.handler(h.event(), req())).toMatchObject({ ok: false, code: 'cooldown' })

    h.setNow(NOW + CANCEL_COOLDOWN_MS)
    expect(await h.handler(h.event(), req())).toMatchObject({ ok: true })
  })

  test('at most 10 confirms per minute', async () => {
    const gate = createConfirmRateGate(() => 0)

    for (let i = 0; i < MAX_CONFIRMS_PER_MINUTE; i++) {
      expect(gate.tryOpen()).toEqual({ ok: true })
      gate.close(false)
    }

    expect(gate.tryOpen()).toEqual({ ok: false, reason: 'rate' })
  })

  test('the per-minute window rolls', () => {
    let t = 0
    const gate = createConfirmRateGate(() => t)

    for (let i = 0; i < MAX_CONFIRMS_PER_MINUTE; i++) {
      gate.tryOpen()
      gate.close(false)
    }

    t = 60_001
    expect(gate.tryOpen()).toEqual({ ok: true })
  })
})

describe('Touch ID for prod scopes', () => {
  const prodReq = () => req({ scope: ['conductor:prod:target'], subject: 'deploy:42' })

  test('prod + Touch ID available: promptTouchID runs after Send; a failure signs nothing', async () => {
    const h = harness()
    h.touchId.canPrompt.mockReturnValue(true)
    h.touchId.prompt.mockRejectedValueOnce(new Error('cancelled'))

    const result = await h.handler(h.event(), prodReq())

    expect(h.touchId.prompt).toHaveBeenCalledTimes(1)
    expect(result).toMatchObject({ ok: false, cancelled: true, reason: 'touch_id' })
    expect(grantFiles(h.grantsDir)).toEqual([])
  })

  test('prod + Touch ID success signs with confirm=touch_id', async () => {
    const h = harness()
    h.touchId.canPrompt.mockReturnValue(true)

    const result: any = await h.handler(h.event(), prodReq())

    expect(result.ok).toBe(true)
    const payload = JSON.parse(Buffer.from(result.envelope.payload, 'base64url').toString('utf8'))
    expect(payload.confirm).toBe('touch_id')
    expect(payload.subject).toEqual({ 'conductor:prod:target': 'deploy:42' })
    expect(h.showMessageBox.mock.calls[0][0].detail).toMatch(/Touch ID/)
  })

  test('a non-prod grant never prompts Touch ID', async () => {
    const h = harness()
    h.touchId.canPrompt.mockReturnValue(true)

    await h.handler(h.event(), req({ scope: ['conductor:gate:review-budget-enable'] }))

    expect(h.touchId.prompt).not.toHaveBeenCalled()
  })

  test('prod without a subject is refused before the dialog', async () => {
    const h = harness()
    const result = await h.handler(h.event(), req({ scope: ['conductor:prod:target'] }))

    expect(result).toMatchObject({ ok: false, code: 'subject_required' })
    expect(h.showMessageBox).not.toHaveBeenCalled()
  })
})

describe('request shape', () => {
  test('composer_signed targets only its own session', async () => {
    const h = harness()

    const ok = await h.handler(
      h.event(),
      req({
        gesture: 'composer_signed',
        origin: { session_id: 'w1', message_id: null, role: 'user' },
        targets: [{ profile: 'default', session_id: 'w1' }]
      })
    )

    expect(ok).toMatchObject({ ok: true, targets: ['default:w1'] })

    const bad = await h.handler(
      h.event(),
      req({
        gesture: 'composer_signed',
        origin: { session_id: 'w1', message_id: null, role: 'user' },
        targets: [{ profile: 'default', session_id: 'w2' }]
      })
    )

    expect(bad).toMatchObject({ ok: false, code: 'self_target' })
  })

  test('junk requests are refused without a dialog', async () => {
    const h = harness()

    for (const bad of [null, 'x', req({ text: 7 }), req({ targets: 'w1' }), req({ gesture: 'widget' })]) {
      expect(await h.handler(h.event(), bad)).toMatchObject({ ok: false })
    }

    expect(h.showMessageBox).not.toHaveBeenCalled()
  })
})
