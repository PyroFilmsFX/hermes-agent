import { beforeEach, describe, expect, it, vi } from 'vitest'

import type * as OpenSessionModule from '@/app/open-session'

vi.mock('@/app/open-session', async importOriginal => {
  const actual = await importOriginal<typeof OpenSessionModule>()

  return { ...actual, openSession: vi.fn() }
})

vi.mock('@/app/chat/composer/focus', () => ({ requestComposerFocus: vi.fn() }))

vi.mock('@/lib/external-link', () => ({ openExternalLink: vi.fn() }))

const { openSession } = await import('@/app/open-session')
const { requestComposerFocus } = await import('@/app/chat/composer/focus')
const { openExternalLink } = await import('@/lib/external-link')
const composer = await import('@/store/composer')
const { $selectedStoredSessionId } = await import('@/store/session')

const {
  conductorOpenIntent,
  conductorTarget,
  githubUrlOf,
  openConductorExternal,
  openConductorTarget,
  sendToConductorTarget
} = await import('./conductor-actions')

const { makeConductorRow } = await import('./conductors-fixtures')

const open = vi.mocked(openSession)
const navigate = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  composer.clearSessionDraft('hs-a')
  $selectedStoredSessionId.set(null)
})

describe('conductorTarget', () => {
  it('names the session, and the owner profile only when it is not the active one', () => {
    expect(conductorTarget(makeConductorRow('a'), 'default')).toEqual({ sessionId: 'hs-a' })
    expect(conductorTarget(makeConductorRow('a', { orchestrator: { profile: null } }), 'default')).toEqual({
      sessionId: 'hs-a'
    })
    expect(conductorTarget(makeConductorRow('a', { orchestrator: { profile: ' writer ' } }), 'default')).toEqual({
      ownerProfile: 'writer',
      sessionId: 'hs-a'
    })
    expect(conductorTarget(makeConductorRow('a', { orchestrator: { profile: 'writer' } }), 'writer')).toEqual({
      sessionId: 'hs-a'
    })
  })

  it('is null for an unattributed row', () => {
    expect(conductorTarget(makeConductorRow('a', { orchestrator: { hermes_session_id: null } }), 'default')).toBeNull()
    expect(conductorTarget(makeConductorRow('a', { orchestrator: { hermes_session_id: '  ' } }), 'default')).toBeNull()
  })
})

describe('conductorOpenIntent', () => {
  const local = { sessionId: 'x' }
  const foreign = { ownerProfile: 'writer', sessionId: 'x' }

  it('plain = stack, ⌘ = tab, ⇧⌘ = window', () => {
    expect(conductorOpenIntent(null, local)).toBe('stack')
    expect(conductorOpenIntent({ shiftKey: true }, local)).toBe('stack')
    expect(conductorOpenIntent({ metaKey: true }, local)).toBe('tab')
    expect(conductorOpenIntent({ ctrlKey: true, shiftKey: true }, local)).toBe('window')
  })

  it('a cross-profile plain open takes a tab (main carries no owner); modifiers still win', () => {
    expect(conductorOpenIntent(null, foreign)).toBe('tab')
    expect(conductorOpenIntent({ metaKey: true, shiftKey: true }, foreign)).toBe('window')
  })

  it('passes the owner profile to openSession as a sessions-mode scope', () => {
    openConductorTarget(foreign, 'tab', navigate)
    openConductorTarget(local, 'stack', navigate)
    expect(open.mock.calls).toEqual([
      ['x', navigate, 'tab', { ownerProfile: 'writer', workspaceMode: 'sessions' }],
      ['x', navigate, 'stack', undefined]
    ])
  })
})

describe('sendToConductorTarget', () => {
  it('stashes the text as the draft and opens the session as a tab — it never submits', () => {
    const stash = vi.spyOn(composer, 'stashSessionDraft')
    expect(sendToConductorTarget({ sessionId: 'hs-a' }, '  ship it  ', navigate)).toBe(true)
    expect(stash).toHaveBeenCalledWith('hs-a', 'ship it', [])
    expect(composer.takeSessionDraft('hs-a').text).toBe('ship it')
    expect(open).toHaveBeenCalledWith('hs-a', navigate, 'tab', undefined)
    stash.mockRestore()
  })

  it('does nothing for blank text', () => {
    expect(sendToConductorTarget({ sessionId: 'hs-a' }, '  \n ', navigate)).toBe(false)
    expect(open).not.toHaveBeenCalled()
    expect(composer.takeSessionDraft('hs-a').text).toBe('')
  })

  it('reloads and focuses a composer already showing that session', () => {
    const sync = vi.spyOn(composer, 'requestComposerDraftSync')
    $selectedStoredSessionId.set('hs-a')
    sendToConductorTarget({ sessionId: 'hs-a' }, 'hi', navigate)
    expect(sync).toHaveBeenCalledWith('reload', 'main')
    expect(requestComposerFocus).toHaveBeenCalledWith('main')
    sync.mockRestore()
  })
})

describe('external links', () => {
  it('only opens https://github.com/ URLs', () => {
    expect(githubUrlOf('https://github.com/o/r/pull/1')).toBe('https://github.com/o/r/pull/1')
    expect(githubUrlOf('http://github.com/o/r')).toBeNull()
    expect(githubUrlOf('https://github.com.evil.example/x')).toBeNull()
    expect(githubUrlOf('javascript:alert(1)')).toBeNull()
    openConductorExternal('https://evil.example/')
    openConductorExternal('https://github.com/o/r/actions/runs/1')
    expect(vi.mocked(openExternalLink).mock.calls).toEqual([['https://github.com/o/r/actions/runs/1']])
  })
})
