import { beforeEach, describe, expect, it, vi } from 'vitest'

// #49 R6: a caller that knows only a session's PROFILE (the Conductors page's
// cross-profile rows) passes `ownerProfile`; a new sessions-mode tile must
// record it so the tile's resume routes to that profile.

const focusOpenSession = vi.fn<(...args: unknown[]) => 'main' | 'tile' | null>(() => null)
const openSessionTile = vi.fn()
const setSessionTileWorkspaceScope = vi.fn()

vi.mock('@/store/session-states', () => ({
  focusedSessionNeedsRoute: (focused: 'main' | 'tile' | null) => !focused,
  focusedSessionWorkspaceScope: () => ({ workspaceMode: 'sessions' }),
  focusOpenSession: (...args: unknown[]) => focusOpenSession(...args),
  openSessionTile: (...args: unknown[]) => openSessionTile(...args),
  reuseBlankDraftTile: () => false,
  setSessionTileWorkspaceScope: (...args: unknown[]) => setSessionTileWorkspaceScope(...args)
}))

vi.mock('@/store/windows', () => ({ canOpenSessionWindow: () => true, openSessionInNewWindow: vi.fn() }))

vi.mock('./routes', () => ({
  $workspaceIsPage: { get: () => false },
  sessionRoute: (id: string) => `/c/${encodeURIComponent(id)}`
}))

const { openSession } = await import('./open-session')

beforeEach(() => {
  vi.clearAllMocks()
})

describe('openSession with an owner profile', () => {
  it('opens the new tile with that profile as its owner', () => {
    const scope = { ownerProfile: 'writer', workspaceMode: 'sessions' as const }
    openSession('s1', () => {}, 'tab', scope)
    expect(setSessionTileWorkspaceScope).toHaveBeenCalledWith('s1', scope)
    expect(openSessionTile).toHaveBeenCalledWith('s1', 'center', undefined, undefined, scope)
  })

  it('leaves the ownerless tab path exactly as it was', () => {
    openSession('s2', () => {}, 'tab')
    expect(openSessionTile).toHaveBeenCalledWith('s2', 'center')
  })
})
