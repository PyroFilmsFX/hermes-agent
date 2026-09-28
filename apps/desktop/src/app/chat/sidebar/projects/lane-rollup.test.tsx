import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesGitWorktree } from '@/global'
import type { SessionInfo } from '@/hermes'
import { $dismissedWorktreeIds, $removedWorktreeIds, $sidebarWorkspaceNodeOpen } from '@/store/layout'
import type * as SessionDotStateMod from '@/store/session-dot-state'

import { RepoFlatSection } from './entered-content'
import { WorktreeLaneRollup } from './lane-rollup'
import type { SidebarSessionGroup, SidebarWorkspaceTree } from './workspace-groups'

afterEach(cleanup)

const { mockDotStates } = vi.hoisted(() => {
  let val: Record<string, any> = {}
  const listeners = new Set<() => void>()

  return {
    mockDotStates: {
      get: () => val,
      listen: (listener: () => void) => {
        listeners.add(listener)

        return () => {
          listeners.delete(listener)
        }
      },
      set: (next: Record<string, any>) => {
        val = next
        listeners.forEach(l => l())
      }
    }
  }
})

vi.mock('@/store/session-dot-state', async importOriginal => {
  const actual = await importOriginal<typeof SessionDotStateMod>()

  return {
    ...actual,
    $sessionDotStateById: mockDotStates
  }
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: {
        cancel: 'Cancel',
        confirm: 'Confirm',
        done: 'Done',
        loading: 'Loading…'
      },
      sidebar: {
        laneRollup: (active: number, done: number, running?: number) =>
          `${active} active · ${done} done${running && running > 0 ? ` · ${running} running` : ''}`,
        laneRollupDone: (count: number) => `Done (${count})`,
        newSessionIn: (label: string) => `New session in ${label}`,
        noSessions: 'No sessions yet',
        projects: {
          enter: (label: string) => `Open ${label}`,
          forceRemove: 'Force remove',
          merged: 'merged',
          menu: 'Actions',
          removeFromSidebar: 'Remove from sidebar',
          removeWorktree: 'Remove worktree',
          removeWorktreeConfirm: 'Remove worktree?',
          removeWorktreeDirty: 'Worktree has changes.',
          removeWorktreeFailed: 'Failed to remove worktree',
          reorder: (label: string) => `Reorder ${label}`,
          reveal: 'Reveal in file manager',
          toggle: (label: string, open: boolean) => `${open ? 'Show' : 'Hide'} ${label} sessions`
        },
        row: {
          sessionActions: 'Session actions'
        },
        showMoreIn: (count: number, label: string) => `Show ${count} more in ${label}`
      },
      statusStack: {
        coding: {
          switchFailed: (label: string) => `Switch failed for ${label}`
        }
      }
    }
  })
}))

vi.mock('@/store/projects', async () => ({
  ...(await vi.importActual('@/store/projects')),
  removeWorktreePath: vi.fn(),
  switchBranchInRepo: vi.fn()
}))

const makeSession = (id: string): SessionInfo =>
  ({
    id,
    message_count: 1,
    model: 'hermes',
    started_at: Date.now()
  }) as SessionInfo

const makeLane = (over: Partial<SidebarSessionGroup> & { id: string; label: string }): SidebarSessionGroup => ({
  path: over.id,
  sessions: [],
  ...over
})

describe('WorktreeLaneRollup', () => {
  beforeEach(() => {
    mockDotStates.set({})
    $sidebarWorkspaceNodeOpen.set({})
  })

  it('renders nothing when lanes is empty', () => {
    const { container } = render(
      <WorktreeLaneRollup
        lanes={[]}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    expect(container.firstChild).toBeNull()
  })

  it('renders "5 active · 0 done" default collapsed, and expands to show 5 lanes', () => {
    const lanes = [
      makeLane({ id: '/repo/.claude/worktrees/lane-1', label: 'lane-1' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-2', label: 'lane-2' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-3', label: 'lane-3' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-4', label: 'lane-4' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-5', label: 'lane-5' })
    ]

    render(
      <WorktreeLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    // Rollup node header is visible
    expect(screen.getByTitle('5 active · 0 done')).toBeTruthy()

    // Default collapsed: child lanes not yet in DOM
    expect(screen.queryByTitle('lane-1')).toBeNull()
    expect(screen.queryByTitle('lane-5')).toBeNull()

    // Click to expand
    fireEvent.click(screen.getByRole('button', { name: /5 active/ }))

    // Now all 5 lanes are rendered
    expect(screen.getByTitle(/lane-1/)).toBeTruthy()
    expect(screen.getByTitle(/lane-2/)).toBeTruthy()
    expect(screen.getByTitle(/lane-3/)).toBeTruthy()
    expect(screen.getByTitle(/lane-4/)).toBeTruthy()
    expect(screen.getByTitle(/lane-5/)).toBeTruthy()
  })

  it('shows merged lanes with a muted flag after unmerged lanes', () => {
    const activeLane = makeLane({ id: '/repo/active', label: 'feature/active' })
    const mergedLane = Object.assign(makeLane({ id: '/repo/merged', label: 'feature/merged' }), { merged: true })

    render(
      <WorktreeLaneRollup
        lanes={[mergedLane, activeLane]}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /2 active/ }))

    const laneTitles = screen.getAllByTitle(/feature\//).map(node => node.getAttribute('title')?.split('\n')[0])

    expect(laneTitles).toEqual(['feature/active', 'feature/merged'])
    expect(screen.getByText('merged')).toBeTruthy()
    expect(screen.getByText('merged').className).toContain('text-(--ui-text-quaternary)')
  })

  it('counts running lanes and renders running dot and running arc when M > 0', () => {
    const lanes = [
      makeLane({
        id: '/repo/.claude/worktrees/lane-1',
        label: 'lane-1',
        sessions: [makeSession('s1'), makeSession('s1-2')]
      }),
      makeLane({
        id: '/repo/.claude/worktrees/lane-2',
        label: 'lane-2',
        sessions: [makeSession('s2')]
      }),
      makeLane({
        id: '/repo/.claude/worktrees/lane-3',
        label: 'lane-3',
        sessions: [makeSession('s3')]
      }),
      makeLane({
        id: '/repo/.claude/worktrees/lane-4',
        label: 'lane-4',
        sessions: []
      }),
      makeLane({
        id: '/repo/.claude/worktrees/lane-5',
        label: 'lane-5',
        sessions: []
      })
    ]

    // s1 is working (lane 1), s2 is stalled (lane 2), s3 is idle (lane 3)
    mockDotStates.set({
      s1: 'working',
      's1-2': 'working', // Multiple running in lane-1 still counts as 1 lane
      s2: 'stalled',
      s3: 'idle'
    })

    const { container } = render(
      <WorktreeLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    // Shows 5 active · 0 done · 2 running
    expect(screen.getByTitle('5 active · 0 done · 2 running')).toBeTruthy()

    // Has running dot and running arc
    expect(container.querySelector('[data-running-dot]')).toBeTruthy()
    expect(container.querySelector('[data-running-arc]')).toBeTruthy()
  })

  it('normalizes repoRoot so "/repo/" and "/repo" share open-state', () => {
    const lanes = [makeLane({ id: '/repo/.claude/worktrees/lane-1', label: 'lane-1' })]

    const { unmount } = render(
      <WorktreeLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo/"
      />
    )

    // Expand via "/repo/"
    fireEvent.click(screen.getByRole('button', { name: /1 active/ }))
    expect($sidebarWorkspaceNodeOpen.get()['/repo::lanes']).toBe(true)
    unmount()

    // Render with "/repo" (no trailing slash) - should already be open
    render(
      <WorktreeLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )
    expect(screen.getByTitle(/lane-1/)).toBeTruthy()
  })
})

describe('WorktreeLaneRollup done lanes (D31)', () => {
  beforeEach(() => {
    mockDotStates.set({})
    $sidebarWorkspaceNodeOpen.set({})
  })

  const doneLane = (id: string, over: Partial<SidebarSessionGroup> = {}): SidebarSessionGroup =>
    makeLane({
      branch: id,
      clean: true,
      id: `/repo/.claude/worktrees/${id}`,
      label: id,
      mergedVia: 'merged-ancestor',
      path: `/repo/.claude/worktrees/${id}`,
      ...over
    })

  it('labels "N active · M done" and nests done lanes in a collapsed Done group, never removing them', () => {
    const lanes = [
      doneLane('done-a'),
      makeLane({ id: '/repo/.claude/worktrees/still-open', label: 'still-open', mergedVia: null }),
      doneLane('done-b', { mergedVia: 'merged-squash', sessions: [makeSession('idle-session')] })
    ]

    mockDotStates.set({ 'idle-session': 'idle' })

    render(
      <WorktreeLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    expect(screen.getByTitle('1 active · 2 done')).toBeTruthy()
    fireEvent.click(screen.getByTitle('1 active · 2 done'))

    // Active lanes list first; the Done group follows them, collapsed.
    const activeLane = screen.getByTitle(/still-open/)
    const doneHeader = screen.getByTitle('Done (2)')

    expect(activeLane.compareDocumentPosition(doneHeader) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTitle(/done-a/)).toBeNull()
    expect(screen.queryByTitle(/done-b/)).toBeNull()

    // Expanding the Done group shows every done lane: collapsed, not removed.
    fireEvent.click(doneHeader)
    expect(screen.getByTitle(/done-a/)).toBeTruthy()
    expect(screen.getByTitle(/done-b/)).toBeTruthy()
  })

  it('renders no Done group while nothing is done', () => {
    render(
      <WorktreeLaneRollup
        lanes={[makeLane({ id: '/repo/wt', label: 'wt' })]}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    fireEvent.click(screen.getByTitle('1 active · 0 done'))
    expect(screen.queryByTitle(/^Done/)).toBeNull()
  })

  it('a done lane is active again the moment a session in it goes live or unread', () => {
    const lanes = [doneLane('lane-x', { sessions: [makeSession('sx')] })]

    render(
      <WorktreeLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    expect(screen.getByTitle('0 active · 1 done')).toBeTruthy()

    act(() => mockDotStates.set({ sx: 'working' }))
    expect(screen.getByTitle('1 active · 0 done · 1 running')).toBeTruthy()

    act(() => mockDotStates.set({ sx: 'needs-input' }))
    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()

    act(() => mockDotStates.set({ sx: 'unread' }))
    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()

    act(() => mockDotStates.set({ sx: 'idle' }))
    expect(screen.getByTitle('0 active · 1 done')).toBeTruthy()
  })

  it('the persisted row.unread keeps a lane active', () => {
    const session = { ...makeSession('su'), unread: true } as SessionInfo

    render(
      <WorktreeLaneRollup
        lanes={[doneLane('lane-u', { sessions: [session] })]}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()
  })

  it('a dirty or unknown worktree is never done', () => {
    render(
      <WorktreeLaneRollup
        lanes={[doneLane('dirty', { clean: false }), doneLane('unknown', { clean: undefined })]}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    expect(screen.getByTitle('2 active · 0 done')).toBeTruthy()
  })
})

describe('RepoFlatSection integration', () => {
  beforeEach(() => {
    mockDotStates.set({})
    $sidebarWorkspaceNodeOpen.set({})
  })

  it('rolls all linked worktrees into one repo row after the home lane', () => {
    const repo: SidebarWorkspaceTree = {
      groups: [
        makeLane({ id: '/repo::branch::main', isHome: true, isMain: true, label: 'main' }),
        makeLane({ id: '/repo/feat-payment', label: 'feat-payment', path: '/repo/feat-payment' }),
        makeLane({ id: '/repo/.claude/worktrees/lane-1', label: 'lane-1', path: '/repo/.claude/worktrees/lane-1' }),
        makeLane({ id: '/repo/.claude/worktrees/lane-2', label: 'lane-2', path: '/repo/.claude/worktrees/lane-2' }),
        makeLane({ id: '/repo/.claude/worktrees/lane-3', label: 'lane-3', path: '/repo/.claude/worktrees/lane-3' }),
        makeLane({ id: '/repo/.claude/worktrees/lane-4', label: 'lane-4', path: '/repo/.claude/worktrees/lane-4' }),
        makeLane({ id: '/repo/.claude/worktrees/lane-5', label: 'lane-5', path: '/repo/.claude/worktrees/lane-5' })
      ],
      id: '/repo',
      label: 'my-repo',
      path: '/repo',
      sessionCount: 0
    }

    render(
      <RepoFlatSection
        onNewSession={vi.fn()}
        renderRows={() => null}
        repo={repo}
        showHeader={false}
      />
    )

    // Home lane stays visible; linked worktrees live in the rollup.
    expect(screen.getByTitle(/main/)).toBeTruthy()
    expect(screen.queryByTitle(/feat-payment/)).toBeNull()

    // Every linked worktree is collapsed into the repo's 6-lane node.
    expect(screen.getByTitle('6 active · 0 done')).toBeTruthy()
    expect(screen.queryByTitle(/feat-payment/)).toBeNull()
    expect(screen.queryByTitle(/lane-1/)).toBeNull()
    expect(screen.queryByTitle(/lane-5/)).toBeNull()

    // Expanding the rollup reveals all six worktrees.
    fireEvent.click(screen.getByRole('button', { name: /6 active/ }))
    expect(screen.getByTitle(/feat-payment/)).toBeTruthy()
    expect(screen.getByTitle(/lane-1/)).toBeTruthy()
    expect(screen.getByTitle(/lane-2/)).toBeTruthy()
    expect(screen.getByTitle(/lane-3/)).toBeTruthy()
    expect(screen.getByTitle(/lane-4/)).toBeTruthy()
    expect(screen.getByTitle(/lane-5/)).toBeTruthy()
  })

  it('a single linked worktree is still grouped in the repo rollup', () => {
    const repo: SidebarWorkspaceTree = {
      groups: [
        makeLane({ id: '/repo::branch::main', isHome: true, isMain: true, label: 'main' }),
        makeLane({ id: '/repo/feat-login', label: 'feat-login', path: '/repo/feat-login' })
      ],
      id: '/repo',
      label: 'my-repo',
      path: '/repo',
      sessionCount: 0
    }

    render(
      <RepoFlatSection
        onNewSession={vi.fn()}
        renderRows={() => null}
        repo={repo}
        showHeader={false}
      />
    )

    expect(screen.getByTitle(/main/)).toBeTruthy()
    expect(screen.queryByTitle(/feat-login/)).toBeNull()
    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()
  })

  it('kanban skip is unchanged', () => {
    const repo: SidebarWorkspaceTree = {
      groups: [
        makeLane({ id: '/repo::branch::main', isHome: true, isMain: true, label: 'main' })
      ],
      id: '/repo',
      label: 'my-repo',
      path: '/repo',
      sessionCount: 0
    }

    const discoveredWorktrees: HermesGitWorktree[] = [
      {
        branch: 't_abc123',
        detached: false,
        isMain: false,
        locked: false,
        path: '/repo/.worktrees/t_abc123'
      },
      {
        branch: 'lane-conductor-1',
        detached: false,
        isMain: false,
        locked: false,
        path: '/repo/.claude/worktrees/lane-conductor-1'
      }
    ]

    render(
      <RepoFlatSection
        discoveredWorktrees={discoveredWorktrees}
        onNewSession={vi.fn()}
        renderRows={() => null}
        repo={repo}
        showHeader={false}
      />
    )

    // Kanban task worktree was skipped by discovery
    expect(screen.queryByTitle(/t_abc123/)).toBeNull()

    // Conductor lane was recognized and placed into 1 lane rollup node
    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()
  })
})

describe('RepoFlatSection dismissed lanes with live activity', () => {
  const laneId = '/repo/.claude/worktrees/hidden'

  const repo: SidebarWorkspaceTree = {
    groups: [
      makeLane({ id: '/repo::branch::main', isHome: true, isMain: true, label: 'main' }),
      makeLane({ id: laneId, label: 'hidden', path: laneId, sessions: [makeSession('sh')] })
    ],
    id: '/repo',
    label: 'my-repo',
    path: '/repo',
    sessionCount: 1
  }

  beforeEach(() => {
    mockDotStates.set({})
    $sidebarWorkspaceNodeOpen.set({})
    $removedWorktreeIds.set([])
    $dismissedWorktreeIds.set([laneId])
  })

  afterEach(() => {
    $dismissedWorktreeIds.set([])
  })

  it('stays hidden while idle and shows again as soon as a session in it is live or unread', () => {
    render(
      <RepoFlatSection
        onNewSession={vi.fn()}
        renderRows={() => null}
        repo={repo}
        showHeader={false}
      />
    )

    expect(screen.queryByTitle(/active ·/)).toBeNull()

    act(() => mockDotStates.set({ sh: 'working' }))
    expect(screen.getByTitle('1 active · 0 done · 1 running')).toBeTruthy()

    act(() => mockDotStates.set({ sh: 'unread' }))
    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()

    act(() => mockDotStates.set({ sh: 'idle' }))
    expect(screen.queryByTitle(/active ·/)).toBeNull()
  })

  it('shows again for a persisted unread row', () => {
    const unreadRepo: SidebarWorkspaceTree = {
      ...repo,
      groups: repo.groups.map(group =>
        group.id === laneId ? { ...group, sessions: [{ ...makeSession('sh'), unread: true } as SessionInfo] } : group
      )
    }

    render(
      <RepoFlatSection
        onNewSession={vi.fn()}
        renderRows={() => null}
        repo={unreadRepo}
        showHeader={false}
      />
    )

    expect(screen.getByTitle('1 active · 0 done')).toBeTruthy()
  })
})
