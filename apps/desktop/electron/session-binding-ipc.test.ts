/**
 * b10 H8: session-binding IPC handlers test suite.
 *
 * Verifies:
 * - untrusted sender refused;
 * - rate gate (dialog_open, cooldown on cancelled re-bind, max confirms per minute);
 * - set with a non-directory / symlink-escaping / non-git path refused;
 * - set probes toplevel+common root and writes a bound record (including worktree support);
 * - renderer-supplied project_root fields are ignored;
 * - re-bind of a live-attested binding asks confirmRebind and aborts on false;
 * - clear writes unbound with seq+1;
 * - status returns the record state incl. needs_reconfirm;
 * - store "signing off" surfaces as an error state, not a throw.
 */

import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, test, vi } from 'vitest'

import { CANCEL_COOLDOWN_MS, MAX_CONFIRMS_PER_MINUTE } from './owner-forward-confirm'
import { createOwnerKeyStore, type SafeStorageLike } from './owner-grant-key'
import {
  createSessionBindingIpcHandlers,
  probeWorkspace,
  type RebindConfirmDetails,
  type SessionBindingIpcDeps
} from './session-binding-ipc'
import { createSessionBindingStore, type SessionBindingStore } from './session-binding-store'

const tmpDirs: string[] = []

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    try {
      fs.rmSync(dir, { recursive: true, force: true })
    } catch {
      // ignore cleanup errors
    }
  }
})

function tmpDir(prefix = 'sb-ipc-'): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  tmpDirs.push(dir)
  return dir
}

const mockSafeStorage: SafeStorageLike = {
  isEncryptionAvailable: () => true,
  encryptString: (plain: string) => Buffer.from('W:' + Buffer.from(plain).toString('base64')),
  decryptString: (wrapped: Buffer) => Buffer.from(wrapped.toString().slice(2), 'base64').toString()
}

function initGitRepo(dir: string, remoteUrl?: string): string {
  fs.mkdirSync(dir, { recursive: true })
  execFileSync('git', ['init'], { cwd: dir, stdio: 'ignore' })
  execFileSync('git', ['config', 'user.name', 'Test User'], { cwd: dir, stdio: 'ignore' })
  execFileSync('git', ['config', 'user.email', 'test@example.com'], { cwd: dir, stdio: 'ignore' })
  execFileSync('git', ['commit', '--allow-empty', '-m', 'initial commit'], { cwd: dir, stdio: 'ignore' })
  if (remoteUrl) {
    execFileSync('git', ['remote', 'add', 'origin', remoteUrl], { cwd: dir, stdio: 'ignore' })
  }
  return fs.realpathSync(dir)
}

function addGitWorktree(mainRepoDir: string, worktreeDir: string, branch = 'wt-test'): string {
  execFileSync('git', ['worktree', 'add', '-b', branch, worktreeDir], { cwd: mainRepoDir, stdio: 'ignore' })
  return fs.realpathSync(worktreeDir)
}

function harness(options: {
  trusted?: boolean
  isAttestationLive?: (profile: string, sid: string) => boolean
  confirmRebind?: (details: RebindConfirmDetails) => Promise<boolean>
  store?: SessionBindingStore
} = {}) {
  const base = tmpDir()
  const keyStore = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
  const info = keyStore.ensure()
  keyStore.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })
  const grantsDir = path.join(base, 'grants')

  const store = options.store ?? createSessionBindingStore({ store: keyStore, grantsDir })
  let currentTime = 100_000

  const trusted = options.trusted ?? true
  const confirmRebind = options.confirmRebind ?? vi.fn(async () => true)
  const isAttestationLive = options.isAttestationLive ?? vi.fn(() => false)

  const deps: SessionBindingIpcDeps = {
    isTrustedSender: event => (event as any)?.trusted !== false && trusted,
    store,
    confirmRebind,
    isAttestationLive,
    now: () => currentTime,
    log: vi.fn()
  }

  const handlers = createSessionBindingIpcHandlers(deps)
  const event = (isTrusted = true) => ({ trusted: isTrusted })

  return {
    base,
    keyStore,
    grantsDir,
    store,
    handlers,
    event,
    confirmRebind,
    isAttestationLive,
    getTime: () => currentTime,
    setNow: (t: number) => {
      currentTime = t
    },
    advanceTime: (dt: number) => {
      currentTime += dt
    }
  }
}

describe('session-binding-ipc: H8 main-side IPC handlers', () => {
  test('untrusted sender refused', async () => {
    const h = harness({ trusted: false })
    const repoDir = initGitRepo(path.join(h.base, 'repo'))

    const setRes = await h.handlers.set(h.event(false), {
      profile: 'default',
      hermes_session_id: 's_untrusted',
      path: repoDir
    })
    expect(setRes).toEqual({ ok: false, reason: 'untrusted_sender' })

    const clearRes = await h.handlers.clear(h.event(false), {
      profile: 'default',
      hermes_session_id: 's_untrusted'
    })
    expect(clearRes).toEqual({ ok: false, reason: 'untrusted_sender' })

    const statusRes = await h.handlers.status(h.event(false), {
      profile: 'default',
      hermes_session_id: 's_untrusted'
    })
    expect(statusRes).toEqual({ ok: false, reason: 'untrusted_sender' })
  })

  test('rate gate: dialog_open, cooldown after cancel, max confirms per minute', async () => {
    let releaseConfirm!: (v: boolean) => void
    let confirmReached!: () => void
    const confirmReachedPromise = new Promise<void>(resolve => {
      confirmReached = resolve
    })
    const pendingConfirm = vi.fn(
      () =>
        new Promise<boolean>(resolve => {
          releaseConfirm = resolve
          confirmReached()
        })
    )

    const h = harness({
      confirmRebind: pendingConfirm,
      isAttestationLive: () => true
    })

    const repoA = initGitRepo(path.join(h.base, 'repoA'))
    const repoB = initGitRepo(path.join(h.base, 'repoB'))

    // 1. Initial bind (first bind -> no native confirmRebind called)
    const firstBind = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_rate',
      path: repoA
    })
    expect('state' in firstBind && firstBind.state).toBe('bound')
    expect(h.confirmRebind).not.toHaveBeenCalled()

    // 2. Start a re-bind that hangs on confirmRebind (dialog open)
    const secondBindPromise = h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_rate',
      path: repoB
    })
    await confirmReachedPromise

    // Concurrent call while dialog is open must fail with dialog_open
    const concurrentCall = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_rate',
      path: repoB
    })
    expect(concurrentCall).toEqual({ ok: false, reason: 'dialog_open' })

    // Cancel the pending confirm
    releaseConfirm(false)
    const secondBind = await secondBindPromise
    expect(secondBind).toEqual({ ok: false, reason: 'cancelled' })

    // 3. Cooldown: calls within CANCEL_COOLDOWN_MS must fail with cooldown
    h.advanceTime(1000)
    const duringCooldown = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_rate',
      path: repoB
    })
    expect(duringCooldown).toEqual({ ok: false, reason: 'cooldown' })

    // Advance time past cooldown
    h.advanceTime(CANCEL_COOLDOWN_MS + 1)

    // 4. Rate limit: at most MAX_CONFIRMS_PER_MINUTE per rolling minute
    // Provide an immediate true confirm for subsequent binds
    ;(h.confirmRebind as any).mockImplementation(async () => true)

    // We had 2 starts in the current window (firstBind + secondBind).
    // Loop until we reach the cap.
    for (let i = 2; i < MAX_CONFIRMS_PER_MINUTE; i++) {
      const res = await h.handlers.set(h.event(), {
        profile: 'default',
        hermes_session_id: `s_rate_${i}`,
        path: repoA
      })
      expect('state' in res && res.state).toBe('bound')
    }

    // Next call must be refused with 'rate'
    const overLimit = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_rate_overflow',
      path: repoA
    })
    expect(overLimit).toEqual({ ok: false, reason: 'rate' })
  })

  test('set with a non-directory / symlink-escaping / non-git path refused', async () => {
    const h = harness()
    const validRepo = initGitRepo(path.join(h.base, 'valid-repo'))

    // Non-directory (regular file)
    const filePath = path.join(h.base, 'regular-file.txt')
    fs.writeFileSync(filePath, 'hello')
    const nonDirRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_test',
      path: filePath
    })
    expect(nonDirRes).toEqual({ ok: false, reason: 'not_a_directory' })

    // Symlink-escaping / broken symlink
    const brokenLink = path.join(h.base, 'broken-link')
    fs.symlinkSync(path.join(h.base, 'non-existent-target'), brokenLink)
    const brokenLinkRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_test',
      path: brokenLink
    })
    expect(brokenLinkRes).toMatchObject({ ok: false, reason: 'symlink_refused' })

    // Non-git directory
    const plainDir = path.join(h.base, 'plain-folder')
    fs.mkdirSync(plainDir)
    const nonGitRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_test',
      path: plainDir
    })
    expect(nonGitRes).toEqual({ ok: false, reason: 'not_git' })

    // Bad path input (empty or NUL)
    const badInputRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_test',
      path: 'invalid\0path'
    })
    expect(badInputRes).toEqual({ ok: false, reason: 'bad_input' })

    // Valid symlink pointing to a git repo should succeed and resolve to realpath
    const symlinkToRepo = path.join(h.base, 'symlink-to-repo')
    fs.symlinkSync(validRepo, symlinkToRepo, 'dir')
    const symlinkRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_symlink',
      path: symlinkToRepo
    })
    expect('state' in symlinkRes && symlinkRes.state).toBe('bound')
    if ('project_root' in symlinkRes) {
      expect(symlinkRes.project_root).toBe(validRepo)
    }
  })

  test('set probes toplevel+common root and writes a bound record', async () => {
    const h = harness()
    const mainRepo = initGitRepo(path.join(h.base, 'main-repo'), 'git@github.com:example/main.git')
    const worktreeDir = path.join(h.base, 'main-worktree')
    const wtPath = addGitWorktree(mainRepo, worktreeDir, 'feat-worktree')

    // Bind to the worktree
    const res = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_wt',
      path: wtPath
    })

    expect('state' in res && res.state).toBe('bound')
    if ('project_root' in res) {
      expect(res.project_root).toBe(wtPath)
      expect(res.repo_common_root).toBe(mainRepo)
      expect(res.repo_remote).toBe('git@github.com:example/main.git')
      expect(res.seq).toBe(1)
      expect(res.binding_nonce).toMatch(/^[A-Z2-7]{26}$/)
    }

    // Verify stored record in store
    const stored = h.store.get('default', 's_wt')
    expect(stored).not.toBeNull()
    expect(stored?.project_root).toBe(wtPath)
    expect(stored?.repo_common_root).toBe(mainRepo)
  })

  test('renderer-supplied project_root fields are ignored', async () => {
    const h = harness()
    const repo = initGitRepo(path.join(h.base, 'real-repo'))

    const res = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_spoof',
      path: repo,
      project_root: '/evil/spoofed/path',
      repo_common_root: '/evil/spoofed/common',
      repo_remote: 'git@evil.com/fake.git',
      seq: 9999,
      binding_nonce: 'SPOOFEDNONCE'
    } as any)

    expect('state' in res && res.state).toBe('bound')
    if ('project_root' in res) {
      expect(res.project_root).toBe(repo)
      expect(res.repo_common_root).toBe(repo)
      expect(res.seq).toBe(1)
      expect(res.binding_nonce).not.toBe('SPOOFEDNONCE')
    }
  })

  test('re-bind of a live-attested binding asks confirmRebind and aborts on false', async () => {
    let isLive = false
    const confirmRebind = vi.fn(async () => false)

    const h = harness({
      isAttestationLive: () => isLive,
      confirmRebind
    })

    const repoA = initGitRepo(path.join(h.base, 'repoA'))
    const repoB = initGitRepo(path.join(h.base, 'repoB'))

    // First bind: no confirmRebind
    const firstRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_live',
      path: repoA
    })
    expect('state' in firstRes && firstRes.state).toBe('bound')
    expect(confirmRebind).not.toHaveBeenCalled()

    // Mark attestation live
    isLive = true

    // Re-bind with confirmRebind returning false -> aborts
    const rebindAborted = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_live',
      path: repoB
    })
    expect(confirmRebind).toHaveBeenCalledTimes(1)
    expect(confirmRebind).toHaveBeenCalledWith(
      expect.objectContaining({
        profile: 'default',
        hermes_session_id: 's_live',
        currentProjectRoot: repoA,
        newProjectRoot: repoB
      })
    )
    expect(rebindAborted).toEqual({ ok: false, reason: 'cancelled' })

    // Store still points to repoA
    const storedAfterCancel = h.store.get('default', 's_live')
    expect(storedAfterCancel?.project_root).toBe(repoA)

    // Advance time past cancel cooldown
    h.advanceTime(CANCEL_COOLDOWN_MS + 10)

    // Now re-bind with confirmRebind returning true -> succeeds
    confirmRebind.mockResolvedValueOnce(true)
    const rebindSuccess = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_live',
      path: repoB
    })
    expect('state' in rebindSuccess && rebindSuccess.state).toBe('bound')
    if ('project_root' in rebindSuccess) {
      expect(rebindSuccess.project_root).toBe(repoB)
      expect(rebindSuccess.seq).toBe(2)
    }

    const storedAfterSuccess = h.store.get('default', 's_live')
    expect(storedAfterSuccess?.project_root).toBe(repoB)
  })

  test('clear writes unbound with seq+1', async () => {
    const h = harness()
    const repo = initGitRepo(path.join(h.base, 'repo'))

    // Initial bind at seq 1
    const bindRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_clear',
      path: repo
    })
    expect('state' in bindRes && bindRes.state).toBe('bound')
    if ('seq' in bindRes) {
      expect(bindRes.seq).toBe(1)
    }

    // Clear binding
    const clearRes = await h.handlers.clear(h.event(), {
      profile: 'default',
      hermes_session_id: 's_clear'
    })

    expect('state' in clearRes && clearRes.state).toBe('unbound')
    if ('seq' in clearRes) {
      expect(clearRes.seq).toBe(2)
      expect(clearRes.project_root).toBeNull()
      expect(clearRes.repo_common_root).toBeNull()
    }

    const stored = h.store.get('default', 's_clear')
    expect(stored?.state).toBe('unbound')
    expect(stored?.seq).toBe(2)
  })

  test('status returns the record state incl. needs_reconfirm', async () => {
    const h = harness()
    const repo = initGitRepo(path.join(h.base, 'repo'))

    // Status for unbound / non-existent session
    const statusEmpty = await h.handlers.status(h.event(), {
      profile: 'default',
      hermes_session_id: 's_none'
    })
    expect(statusEmpty).toMatchObject({
      ok: true,
      state: 'unbound',
      profile: 'default',
      hermes_session_id: 's_none',
      seq: 0,
      project_root: null
    })

    // Status for bound session
    await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_bound',
      path: repo
    })
    const statusBound = await h.handlers.status(h.event(), {
      profile: 'default',
      hermes_session_id: 's_bound'
    })
    expect(statusBound).toMatchObject({
      ok: true,
      state: 'bound',
      profile: 'default',
      hermes_session_id: 's_bound',
      project_root: repo,
      seq: 1
    })

    // Status for record with needs_reconfirm
    // Manually inject a needs_reconfirm record into the store's internal memory
    const mockStore: SessionBindingStore = {
      ...h.store,
      get: (profile, sid) => {
        if (sid === 's_retired') {
          return {
            ok: true,
            state: 'needs_reconfirm',
            profile,
            hermes_session_id: sid,
            seq: 3,
            binding_nonce: 'NONCE123',
            bound_at: 123456,
            project_root: repo,
            repo_common_root: repo,
            repo_remote: null,
            project_id: null,
            carried_from: null,
            envelope: {} as any,
            payload: {} as any,
            verified: false
          }
        }
        return h.store.get(profile, sid)
      }
    }

    const hNeedsReconfirm = harness({ store: mockStore })
    const statusReconfirm = await hNeedsReconfirm.handlers.status(hNeedsReconfirm.event(), {
      profile: 'default',
      hermes_session_id: 's_retired'
    })
    expect(statusReconfirm).toMatchObject({
      ok: true,
      state: 'needs_reconfirm',
      profile: 'default',
      hermes_session_id: 's_retired',
      project_root: repo
    })
  })

  test('store "signing off" surfaces as an error state, not a throw', async () => {
    // Construct store with unanchored key store (signing not allowed)
    const base = tmpDir()
    const unanchoredKeyStore = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
    unanchoredKeyStore.ensure()
    // Do NOT set anchor, so assertMaySign / signDomain fails
    const grantsDir = path.join(base, 'grants')
    const store = createSessionBindingStore({ store: unanchoredKeyStore, grantsDir })

    const h = harness({ store })
    const repo = initGitRepo(path.join(h.base, 'repo'))

    // set should surface error state, not throw
    const setRes = await h.handlers.set(h.event(), {
      profile: 'default',
      hermes_session_id: 's_signoff',
      path: repo
    })
    expect(setRes).toEqual({ ok: false, reason: 'signing_off' })

    // clear should surface error state, not throw
    const clearRes = await h.handlers.clear(h.event(), {
      profile: 'default',
      hermes_session_id: 's_signoff'
    })
    expect(clearRes).toEqual({ ok: false, reason: 'signing_off' })
  })

  test('probeWorkspace returns correct git metadata directly', async () => {
    const base = tmpDir()
    const repo = initGitRepo(path.join(base, 'standalone-repo'), 'https://github.com/org/standalone.git')
    const probed = await probeWorkspace(repo)
    expect('project_root' in probed && probed.project_root).toBe(repo)
    expect('repo_common_root' in probed && probed.repo_common_root).toBe(repo)
    expect('repo_remote' in probed && probed.repo_remote).toBe('https://github.com/org/standalone.git')
  })
})
