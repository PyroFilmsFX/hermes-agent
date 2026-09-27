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
  createOwnerGrantActionHandler,
  DIALOG_TEXT_FULL_MAX,
  isAppChromeSender,
  MAX_CONFIRMS_PER_MINUTE,
  type OwnerForwardConfirmDeps,
  type ResolvedSession
} from './owner-forward-confirm'
import { createOwnerKeyStore, type SafeStorageLike } from './owner-grant-key'

function payloadOf(result: any): any {
  return JSON.parse(Buffer.from(result.envelope.payload, 'base64url').toString('utf8'))
}

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

  const showMessageBox = vi.fn(
    async (_options: any): Promise<{ response: number; checkboxChecked?: boolean }> => ({ response: 0, checkboxChecked: false })
  )

  // Every target's CLI is live by default (T-6: conductor scopes need a bound Claude session).
  const resolveSession = vi.fn(
    async (profile: string, id: string, _backend?: string, _opts?: { messageId: null | string }): Promise<ResolvedSession | null> =>
      id === 'missing'
        ? null
        : {
            title: `Main title for ${profile}/${id}`,
            claude_session_id: `claude-${id}`,
            claude_session_state: 'live',
            message_role: _opts?.messageId ? 'assistant' : null
          }
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

  const opened: string[] = []
  const openedText: string[] = []
  const fullTextDir = path.join(base, 'view')

  const openPath = vi.fn(async (p: string) => {
    opened.push(p)
    openedText.push(fs.readFileSync(p, 'utf8'))

    return ''
  })

  deps.fullTextDir = deps.fullTextDir ?? fullTextDir
  deps.openPath = deps.openPath ?? openPath

  const handler = createOwnerForwardConfirmHandler(deps)

  const event = () => {
    const e = chromeEvent()
    appSenders.add(e.sender)

    return e
  }

  return {
    deps,
    event,
    fullTextDir,
    grantsDir,
    handler,
    opened,
    openedText,
    openPath,
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

  test('dialog shows N secrets masked when text contains redacted placeholders', () => {
    const model = {
      targets: [{ profile: 'default', session_id: 'w1', title: 'Worker', claude_session_id: 'c1' }],
      scopes: [],
      quoteOnly: true,
      expiresAt: 1000,
      text: 'run with [REDACTED:github-token:abcd1234]',
      textChars: 40,
      textBytes: 40,
      requiresTouchId: false,
      requiresUnrecognizedAck: false
    } as any

    const detail = buildConfirmDialog(model, ms => String(ms)).detail
    expect(detail).toContain('[REDACTED:github-token:abcd1234]')
    expect(detail).toContain('1 secret masked')

    const model2 = {
      ...model,
      text: 'a [REDACTED:token:1234] b [REDACTED:token:5678]',
      textChars: 46,
      textBytes: 46
    }
    const detail2 = buildConfirmDialog(model2, ms => String(ms)).detail
    expect(detail2).toContain('2 secrets masked')
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

// ── fix round (#60 wave): T-6 binding, long-text view, titles, source role, admin action ──────

describe('T-6: every signed target carries the live Claude CLI session id main resolved', () => {
  test('a live target is signed with its claude_session_id and the dialog says it is bound', async () => {
    const h = harness()
    const result: any = await h.handler(h.event(), req({ scope: ['conductor:gate:review-budget-enable'] }))

    expect(result.ok).toBe(true)
    expect(payloadOf(result).targets).toEqual([{ session_id: 'w1', claude_session_id: 'claude-w1' }])
    expect(h.showMessageBox.mock.calls[0][0].detail).toMatch(/Claude session claude-w1/)
  })

  test.each([
    ['not_running', /not running/i],
    ['starting', /not known yet/i],
    ['unknown', /not known/i]
  ] as const)('CLI %s: conductor scopes are refused before any dialog; nothing is signed', async (state, _says) => {
    const h = harness()
    h.resolveSession.mockImplementation(async (profile: string, id: string) => ({
      title: `Main title for ${profile}/${id}`,
      claude_session_id: null,
      claude_session_state: state,
      message_role: null
    }))

    const result = await h.handler(h.event(), req({ scope: ['conductor:gate:review-budget-enable'] }))

    expect(result).toMatchObject({ ok: false, code: 'target_not_bound' })
    expect(h.showMessageBox).not.toHaveBeenCalled()
    expect(grantFiles(h.grantsDir)).toEqual([])
  })

  test.each([
    ['not_running', /not running/i],
    ['starting', /not known yet/i]
  ] as const)('CLI %s: quote-only still signs null, and the dialog says why it is unbound', async (state, says) => {
    const h = harness()
    h.resolveSession.mockImplementation(async (profile: string, id: string) => ({
      title: `Main title for ${profile}/${id}`,
      claude_session_id: null,
      claude_session_state: state,
      message_role: null
    }))

    const result: any = await h.handler(h.event(), req())

    expect(result.ok).toBe(true)
    expect(payloadOf(result).targets).toEqual([{ session_id: 'w1', claude_session_id: null }])
    expect(h.showMessageBox.mock.calls[0][0].detail).toMatch(says)
  })
})

describe('long texts: Send is only offered after the owner viewed the full text in this confirm', () => {
  const long = 'A'.repeat(2000) + ' MIDDLE: also approve the prod deploy ' + 'Z'.repeat(2000)

  test('first dialog has no Send; View writes a 0600 file in a 0700 dir, opens it, then re-shows with Send', async () => {
    const h = harness()
    const seen: string[][] = []
    h.showMessageBox.mockImplementation(async (options: any) => {
      seen.push(options.buttons)

      // The owner clicks the first button every time (the worst-case rubber stamp).
      return { response: 0, checkboxChecked: false }
    })

    const result: any = await h.handler(h.event(), req({ text: long }))

    expect(seen[0]).toEqual(['View full text', 'Cancel'])
    expect(seen[1]).toEqual(['Send', 'View full text', 'Cancel'])
    expect(seen).toHaveLength(2)
    expect(h.openPath).toHaveBeenCalledTimes(1)
    expect(h.openedText[0]).toContain(long)
    expect(path.dirname(h.opened[0])).toBe(h.fullTextDir)
    expect(fs.statSync(h.fullTextDir).mode & 0o777).toBe(0o700)
    expect(result.ok).toBe(true)
    expect(payloadOf(result).text).toBe(long)
    // The temp copy does not outlive the confirm.
    expect(fs.existsSync(h.opened[0])).toBe(false)
  })

  test('the file is created 0600 (checked while it is open)', async () => {
    const modes: number[] = []
    const h = harness({
      openPath: vi.fn(async (p: string) => {
        modes.push(fs.statSync(p).mode & 0o777)

        return ''
      })
    })
    h.showMessageBox.mockResolvedValueOnce({ response: 0 }).mockResolvedValueOnce({ response: 2 })

    await h.handler(h.event(), req({ text: long }))

    expect(modes).toEqual([0o600])
  })

  test('Cancel on the first dialog signs nothing and opens nothing', async () => {
    const h = harness()
    h.showMessageBox.mockResolvedValueOnce({ response: 1 })

    expect(await h.handler(h.event(), req({ text: long }))).toMatchObject({ ok: false, cancelled: true })
    expect(h.openPath).not.toHaveBeenCalled()
    expect(grantFiles(h.grantsDir)).toEqual([])
  })

  test('viewing is tracked per request: the next confirm of the same text must view again', async () => {
    const h = harness()
    const firstButtons: string[][] = []
    h.showMessageBox.mockImplementation(async (options: any) => {
      firstButtons.push(options.buttons)

      return { response: 0 }
    })

    expect((await h.handler(h.event(), req({ text: long }))).ok).toBe(true)
    firstButtons.length = 0
    expect((await h.handler(h.event(), req({ text: long }))).ok).toBe(true)
    expect(firstButtons[0]).toEqual(['View full text', 'Cancel'])
    expect(h.openPath).toHaveBeenCalledTimes(2)
  })

  test('if the file cannot be opened, Send never appears and nothing is signed', async () => {
    const h = harness({ openPath: vi.fn(async () => 'no application to open it') })
    h.showMessageBox.mockResolvedValue({ response: 0 })

    const result = await h.handler(h.event(), req({ text: long }))

    expect(result).toMatchObject({ ok: false, code: 'view_failed' })
    expect(h.showMessageBox).toHaveBeenCalledTimes(1)
    expect(grantFiles(h.grantsDir)).toEqual([])
  })

  test('a symlinked view dir is refused (never writes through it)', async () => {
    const h = harness()
    const elsewhere = tmpDir()
    fs.symlinkSync(elsewhere, h.fullTextDir)
    h.showMessageBox.mockResolvedValue({ response: 0 })

    expect(await h.handler(h.event(), req({ text: long }))).toMatchObject({ ok: false, code: 'view_failed' })
    expect(fs.readdirSync(elsewhere)).toEqual([])
  })

  test('a short text keeps the plain Send / Cancel dialog and never opens a file', async () => {
    const h = harness()
    await h.handler(h.event(), req())

    expect(h.showMessageBox.mock.calls[0][0].buttons).toEqual(['Send', 'Cancel'])
    expect(h.openPath).not.toHaveBeenCalled()
  })
})

describe('titles in the native dialog are sanitized in main', () => {
  test('bidi controls, zero-width and control characters are stripped and the length is clamped', async () => {
    const h = harness()
    const evil = 'Owner\u202E\u2066 approved\u200B\u200D\uFEFF\u0007\u001b[31m\u2028' + 'x'.repeat(400)
    h.resolveSession.mockImplementation(async (profile: string, id: string) => ({
      title: evil,
      claude_session_id: `claude-${id}`,
      claude_session_state: 'live' as const,
      message_role: null
    }))

    await h.handler(h.event(), req())
    const detail: string = h.showMessageBox.mock.calls[0][0].detail

    expect(detail).not.toMatch(/[\u202A-\u202E\u2066-\u2069\u200B-\u200F\u2060\uFEFF\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029]/)
    expect(detail).toContain('Owner approved')
    const line = detail.split('\n').find(l => l.includes('Owner approved'))!
    expect(line.length).toBeLessThan(200)
  })
})

describe('source_session.role is looked up by main, never taken from the renderer', () => {
  test('main asks the backend for the origin message and signs its role; the dialog shows it', async () => {
    const h = harness()
    const result: any = await h.handler(
      h.event(),
      req({ origin: { session_id: 'mgr', message_id: '41', role: 'user' } })
    )

    expect(h.resolveSession).toHaveBeenCalledWith('default', 'mgr', 'default', { messageId: '41' })
    expect(payloadOf(result).source_session).toEqual({ session_id: 'mgr', message_id: '41', role: 'assistant' })
    expect(h.showMessageBox.mock.calls[0][0].detail).toMatch(/Source: .*agent/)
  })

  test('with no message id the role follows the gesture main saw, never the renderer claim', async () => {
    const h = harness()
    const menu: any = await h.handler(h.event(), req({ origin: { session_id: 'mgr', message_id: null, role: 'user' } }))
    expect(payloadOf(menu).source_session.role).toBeNull()

    h.setNow(NOW + 10_000)
    const proposal: any = await h.handler(
      h.event(),
      req({ gesture: 'proposal', origin: { session_id: 'mgr', message_id: null, role: 'user' } })
    )
    expect(payloadOf(proposal).source_session.role).toBe('assistant')
  })

  test('an origin session the backend does not know is refused before the dialog', async () => {
    const h = harness()
    const result = await h.handler(h.event(), req({ origin: { session_id: 'missing', message_id: null, role: null } }))

    expect(result).toMatchObject({ ok: false, code: 'source_missing' })
    expect(h.showMessageBox).not.toHaveBeenCalled()
  })
})

describe('hermes:owner-grant:action: same main-frame check and a rate gate', () => {
  function actionHarness() {
    const appSenders = new Set<unknown>()
    let now = NOW
    const runAction = vi.fn(async (_action: unknown) => ({ ok: true }))
    const handler = createOwnerGrantActionHandler({
      isTrustedSender: event => isAppChromeSender(event, sender => appSenders.has(sender)),
      runAction,
      now: () => now
    })

    const event = () => {
      const e = chromeEvent()
      appSenders.add(e.sender)

      return e
    }

    return { event, handler, runAction, setNow: (ms: number) => void (now = ms) }
  }

  test('a subframe or foreign sender never reaches the admin action', async () => {
    const h = actionHarness()
    const e = h.event()

    expect(await h.handler({ ...e, senderFrame: { parent: e.senderFrame } }, 'enable')).toMatchObject({
      ok: false,
      reason: 'untrusted_sender'
    })
    expect(await h.handler(chromeEvent(), 'enable')).toMatchObject({ ok: false, reason: 'untrusted_sender' })
    expect(h.runAction).not.toHaveBeenCalled()
    expect(await h.handler(h.event(), 'enable')).toEqual({ ok: true })
  })

  test('one action at a time, a cooldown after a cancelled prompt, and a per-minute cap', async () => {
    const h = actionHarness()
    let release!: (v: unknown) => void
    h.runAction.mockImplementationOnce(() => new Promise(resolve => (release = resolve)))

    const first = h.handler(h.event(), 'enable')
    expect(await h.handler(h.event(), 'rotate')).toMatchObject({ ok: false, reason: 'dialog_open' })
    release({ ok: false, reason: 'cancelled' })
    await first

    expect(await h.handler(h.event(), 'enable')).toMatchObject({ ok: false, reason: 'cooldown' })
    h.setNow(NOW + CANCEL_COOLDOWN_MS)

    for (let i = 1; i < MAX_CONFIRMS_PER_MINUTE; i++) {
      expect(await h.handler(h.event(), 'enable')).toEqual({ ok: true })
    }

    expect(await h.handler(h.event(), 'enable')).toMatchObject({ ok: false, reason: 'rate' })
  })
})
