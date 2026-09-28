/**
 * Live RCA 2026-09-28 (#60): "Forward to…" from the manager session (thinkbot profile, a Claude Agent
 * SDK session) to cntrl-core-worker delivered nothing, wrote no grant file, and left no log line.
 *
 * Main's half of that path, with the owner's exact request shape:
 * - every refusal, cancel, failure and success leaves one `[owner-forward]` line, codes only (never the
 *   text); before this fix none of the pre-dialog refusals logged anything;
 * - a session lookup that fails (timeout, 5xx) is `lookup_failed`, not "no session …";
 * - the native confirm gets the IPC event, so main parents it to the window that asked;
 * - main signs with the key enrolled AFTER the backend spawned (nothing reads the spawn-time env).
 *
 * Fakes only: no dialog, no Keychain (mock safeStorage), grants in mkdtemp dirs.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  createOwnerForwardConfirmHandler,
  isAppChromeSender,
  OwnerForwardLookupError,
  type OwnerForwardConfirmDeps,
  type ResolvedSession
} from './owner-forward-confirm'
import { createOwnerKeyStore, type SafeStorageLike } from './owner-grant-key'

const ORIGIN = '20260909_193713_ce3d96'
const TARGET = '20260924_200237_d5276f'
const TEXT =
  'approve: merge w6/wd-ci-hold (both commits), and add the drafted PR/CI discipline rules to CLAUDE.md/AGENTS.md'

const tmpDirs: string[] = []

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

function harness(overrides: Partial<OwnerForwardConfirmDeps> = {}, opts: { enrolled?: boolean } = {}) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'ofc-rca-'))
  tmpDirs.push(base)
  const store = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })

  const enroll = () => {
    const info = store.ensure()
    store.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })
  }

  if (opts.enrolled !== false) {
    enroll()
  }

  const grantsDir = path.join(base, 'grants')
  const logs: string[] = []
  const frame = { parent: null }
  const sender = { mainFrame: frame, getType: () => 'window' }
  const event = { sender, senderFrame: frame }

  const showMessageBox = vi.fn(async (_options: any, _event?: unknown) => ({ response: 0, checkboxChecked: false }))

  const resolveSession = vi.fn(
    async (_profile: string, id: string): Promise<ResolvedSession | null> => ({
      title: id === ORIGIN ? 'manager' : 'cntrl-core-worker',
      // The worker is mid-turn in its SDK lane: no announced CLI id (quote-only needs none).
      claude_session_id: null,
      claude_session_state: 'starting',
      message_role: null
    })
  )

  const deps: OwnerForwardConfirmDeps = {
    isTrustedSender: e => isAppChromeSender(e, s => s === sender),
    // main.ts keys this map by the profile each backend was SPAWNED for (here the primary, thinkbot).
    backendIdForProfile: profile => (profile === 'thinkbot' ? 'spawn_live' : null),
    resolveSession,
    showMessageBox,
    store,
    grantsDir,
    ownerUid: 501,
    now: () => 1_790_629_000_000,
    formatTime: ms => `T${ms}`,
    log: message => logs.push(message),
    ...overrides
  }

  return { deps, enroll, event, grantsDir, handler: createOwnerForwardConfirmHandler(deps), logs, resolveSession, showMessageBox }
}

function ownerRequest(overrides: Record<string, unknown> = {}) {
  // Exactly what ForwardSheet → sendOwnerForward sends for "Forward to…" on a selection.
  return {
    text: TEXT,
    gesture: 'selection',
    origin: { session_id: ORIGIN, message_id: null, role: 'assistant' },
    profile: 'thinkbot',
    targets: [{ profile: 'thinkbot', session_id: TARGET }],
    scope: [],
    ttlMs: 7 * 24 * 60 * 60 * 1000,
    ...overrides
  }
}

function grantFiles(dir: string): string[] {
  return fs.existsSync(dir) ? fs.readdirSync(dir).filter(name => name.endsWith('.json')) : []
}

describe("the owner's forward: signed, written, logged", () => {
  test('signs a quote-only grant for an unbound target, writes the file, and logs one signed line', async () => {
    const h = harness()
    const result: any = await h.handler(h.event, ownerRequest())

    expect(result.ok).toBe(true)
    expect(result.targets).toEqual([`thinkbot:${TARGET}`])
    expect(grantFiles(h.grantsDir)).toHaveLength(1)
    expect(h.logs).toEqual(['[owner-forward] confirm signed: 1 grant(s), 1 target(s)'])
  })

  test('the native confirm receives the IPC event (main parents it to the asking window)', async () => {
    const h = harness()
    await h.handler(h.event, ownerRequest())

    expect(h.showMessageBox).toHaveBeenCalledTimes(1)
    expect(h.showMessageBox.mock.calls[0][1]).toBe(h.event)
  })

  test('a key enrolled after the backend spawned signs without any restart (no spawn-env dependency)', async () => {
    const h = harness({}, { enrolled: false })
    const before: any = await h.handler(h.event, ownerRequest())

    expect(before).toMatchObject({ ok: false, code: 'sign_failed' })
    expect(h.logs).toEqual(['[owner-forward] confirm failed: sign_failed (no_key)'])

    h.enroll() // the settings "Let conductor verify owner decisions" enable, while the app keeps running
    const after: any = await h.handler(h.event, ownerRequest())

    expect(after.ok).toBe(true)
    expect(grantFiles(h.grantsDir)).toHaveLength(1)
  })
})

describe('every refusal before the dialog is logged with its code, never the text', () => {
  const cases: Array<[string, (h: ReturnType<typeof harness>) => Promise<any>, string]> = [
    ['no_backend', h => h.handler(h.event, ownerRequest({ profile: 'default' })), 'no_backend'],
    ['bad_request', h => h.handler(h.event, ownerRequest({ targets: [] })), 'bad_request'],
    ['bad_text', h => h.handler(h.event, ownerRequest({ text: `/${TEXT}` })), 'bad_text'],
    ['untrusted_sender', h => h.handler({ sender: {}, senderFrame: {} }, ownerRequest()), 'untrusted_sender']
  ]

  for (const [name, run, code] of cases) {
    test(name, async () => {
      const h = harness()
      const result = await run(h)

      expect(result).toMatchObject({ ok: false, code })
      expect(h.logs).toEqual([`[owner-forward] confirm refused: ${code}`])
      expect(h.logs.join('\n')).not.toContain('approve: merge')
      expect(grantFiles(h.grantsDir)).toEqual([])
      expect(h.showMessageBox).not.toHaveBeenCalled()
    })
  }

  test('source_missing and target_missing (a 404 from main’s own lookup)', async () => {
    const source = harness({ resolveSession: vi.fn(async () => null) })
    expect(await source.handler(source.event, ownerRequest())).toMatchObject({ code: 'source_missing' })
    expect(source.logs).toEqual(['[owner-forward] confirm refused: source_missing'])

    const target = harness({
      resolveSession: vi.fn(async (_p: string, id: string) =>
        id === ORIGIN ? { title: 'manager', claude_session_id: null, message_role: null } : null
      )
    })

    expect(await target.handler(target.event, ownerRequest())).toMatchObject({ code: 'target_missing' })
    expect(target.logs).toEqual(['[owner-forward] confirm refused: target_missing'])
  })

  test('a lookup that fails (timeout / 5xx) is lookup_failed, never "no session"', async () => {
    const h = harness({
      resolveSession: vi.fn(async () => {
        throw new OwnerForwardLookupError('timeout')
      })
    })

    const result: any = await h.handler(h.event, ownerRequest())

    expect(result).toMatchObject({ ok: false, code: 'lookup_failed' })
    expect(result.error).toContain('timeout')
    expect(h.logs).toEqual(['[owner-forward] confirm refused: lookup_failed'])
  })

  test('the rate gate refusal while a confirm is open is logged', async () => {
    let release: (v: { response: number }) => void = () => {}
    const h = harness({ showMessageBox: vi.fn(() => new Promise<{ response: number }>(r => (release = r))) })
    const first = h.handler(h.event, ownerRequest())
    await vi.waitFor(() => expect(h.deps.showMessageBox).toHaveBeenCalled())

    expect(await h.handler(h.event, ownerRequest())).toMatchObject({ code: 'dialog_open' })
    expect(h.logs).toContain('[owner-forward] confirm refused: dialog_open')
    release({ response: 1 })
    expect(await first).toMatchObject({ cancelled: true, reason: 'dialog' })
    expect(h.logs).toContain('[owner-forward] confirm cancelled: dialog')
  })
})
