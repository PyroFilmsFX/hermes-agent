import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopSessionBindingRecord } from '@/global'

vi.mock('@/store/gateway', () => ({ activeGateway: () => null }))

import {
  $sessionBindings,
  $sessionBindingSuggestions,
  clearSessionBinding,
  ensureSessionBinding,
  ensureSessionBindingSuggestion,
  refreshSessionBinding,
  resetSessionBindingsForTests,
  sessionBindingKey,
  setSessionBinding,
  STATUS_MAX_AGE_MS,
  suggestionFromSession
} from './session-binding'

function record(overrides: Partial<DesktopSessionBindingRecord> = {}): DesktopSessionBindingRecord {
  return {
    ok: true,
    state: 'unbound',
    profile: 'default',
    hermes_session_id: 's1',
    seq: 0,
    binding_nonce: null,
    bound_at: null,
    project_root: null,
    repo_common_root: null,
    repo_remote: null,
    ...overrides
  }
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

let status: ReturnType<typeof vi.fn>
let set: ReturnType<typeof vi.fn>
let clear: ReturnType<typeof vi.fn>

beforeEach(() => {
  resetSessionBindingsForTests()
  status = vi.fn(async () => record())
  set = vi.fn(async () => record({ state: 'bound', project_root: '/r/app', seq: 1 }))
  clear = vi.fn(async () => record({ seq: 2 }))
  ;(window as any).hermesDesktop = { sessionBinding: { set, clear, status } }
})

afterEach(() => {
  delete (window as any).hermesDesktop
})

describe('session-binding store', () => {
  it('caches status per (profile, session): a second ensure does not re-ask main', async () => {
    ensureSessionBinding('default', 's1')
    await flush()
    ensureSessionBinding('default', 's1')
    ensureSessionBinding(null, 's1')
    await flush()

    expect(status).toHaveBeenCalledTimes(1)
    expect(status).toHaveBeenCalledWith({ profile: 'default', hermes_session_id: 's1' })
    expect($sessionBindings.get()[sessionBindingKey('default', 's1')]).toMatchObject({ status: 'ready' })

    // A different profile is a different key.
    ensureSessionBinding('work', 's1')
    await flush()
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('re-reads a stale entry on the next session change', async () => {
    ensureSessionBinding('default', 's1')
    await flush()
    ensureSessionBinding('default', 's1', Date.now() + STATUS_MAX_AGE_MS + 1)
    await flush()

    expect(status).toHaveBeenCalledTimes(2)
  })

  it('refreshes after set, sending only {profile, hermes_session_id, path}', async () => {
    ensureSessionBinding('default', 's1')
    await flush()
    status.mockResolvedValueOnce(record({ state: 'bound', project_root: '/r/app', seq: 1 }))

    const outcome = await setSessionBinding({
      profile: 'default',
      hermes_session_id: 's1',
      path: '/r/app/src',
      // A stray field from a caller must never reach main.
      ...({ project_root: '/evil' } as object)
    } as any)

    expect(outcome.ok).toBe(true)
    expect(set).toHaveBeenCalledWith({ profile: 'default', hermes_session_id: 's1', path: '/r/app/src' })
    expect(Object.keys(set.mock.calls[0][0]).sort()).toEqual(['hermes_session_id', 'path', 'profile'])
    expect(status).toHaveBeenCalledTimes(2)
    expect($sessionBindings.get()[sessionBindingKey('default', 's1')]?.record?.state).toBe('bound')
  })

  it('refreshes after clear', async () => {
    status.mockResolvedValueOnce(record({ state: 'bound', project_root: '/r/app', seq: 1 }))
    await setSessionBinding({ profile: 'default', hermes_session_id: 's1', path: '/r/app' })
    expect($sessionBindings.get()[sessionBindingKey('default', 's1')]?.record?.state).toBe('bound')
    status.mockResolvedValueOnce(record({ seq: 2 }))

    await clearSessionBinding({ profile: 'default', hermes_session_id: 's1' })

    expect(clear).toHaveBeenCalledWith({ profile: 'default', hermes_session_id: 's1' })
    expect($sessionBindings.get()[sessionBindingKey('default', 's1')]?.record?.state).toBe('unbound')
  })

  it('keeps the last good record when a read is refused (rate gate), and never overlaps IPC calls', async () => {
    let active = 0
    let maxActive = 0

    status.mockImplementation(async () => {
      active += 1
      maxActive = Math.max(maxActive, active)
      await flush()
      active -= 1

      return record({ state: 'bound', project_root: '/r/app' })
    })

    await Promise.all([refreshSessionBinding('default', 's1'), refreshSessionBinding('default', 's2')])
    expect(maxActive).toBe(1)

    status.mockResolvedValueOnce({ ok: false, reason: 'rate' } as any)
    await refreshSessionBinding('default', 's1')

    const entry = $sessionBindings.get()[sessionBindingKey('default', 's1')]
    expect(entry).toMatchObject({ status: 'error', reason: 'rate' })
    expect(entry?.record?.state).toBe('bound')
  })

  it('does nothing without the IPC bridge', async () => {
    delete (window as any).hermesDesktop
    ensureSessionBinding('default', 's1')
    await flush()

    expect($sessionBindings.get()).toEqual({})
    await expect(setSessionBinding({ profile: 'default', hermes_session_id: 's1', path: '/x' })).resolves.toEqual({
      ok: false,
      reason: 'unavailable'
    })
  })
})

describe('suggestion', () => {
  it('comes from the recorded git repo and cwd; a non-git cwd suggests nothing', () => {
    expect(
      suggestionFromSession({ id: 's1', cwd: '/r/app/.worktrees/x', git_repo_root: '/r/app', git_branch: 'lane/x' })
    ).toEqual({ path: '/r/app/.worktrees/x', name: 'app', branch: 'lane/x' })
    expect(suggestionFromSession({ id: 's1', cwd: '/tmp/notes', git_repo_root: null })).toBeNull()
  })

  it('is resolved once per session', () => {
    ensureSessionBindingSuggestion({ id: 's1', cwd: '/r/app', git_repo_root: '/r/app', profile: 'default' })
    ensureSessionBindingSuggestion({ id: 's1', cwd: '/elsewhere', git_repo_root: '/elsewhere', profile: 'default' })

    expect($sessionBindingSuggestions.get()[sessionBindingKey('default', 's1')]?.path).toBe('/r/app')
  })
})
