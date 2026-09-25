import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesGitWorktree } from '@/global'
import type { SessionInfo } from '@/hermes'
import { $sidebarWorkspaceNodeOpen } from '@/store/layout'
import type * as SessionDotStateMod from '@/store/session-dot-state'

import { RepoFlatSection } from './entered-content'
import { ConductorLaneRollup } from './lane-rollup'
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
        laneRollup: (count: number, running?: number) =>
          `${count} ${count === 1 ? 'lane' : 'lanes'}${running && running > 0 ? ` · ${running} running` : ''}`,
        newSessionIn: (label: string) => `New session in ${label}`,
        noSessions: 'No sessions yet',
        projects: {
          enter: (label: string) => `Open ${label}`,
          forceRemove: 'Force remove',
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

describe('ConductorLaneRollup', () => {
  beforeEach(() => {
    mockDotStates.set({})
    $sidebarWorkspaceNodeOpen.set({})
  })

  it('renders nothing when lanes is empty', () => {
    const { container } = render(
      <ConductorLaneRollup
        lanes={[]}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    expect(container.firstChild).toBeNull()
  })

  it('renders "5 lanes" default collapsed, and expands to show 5 lanes', () => {
    const lanes = [
      makeLane({ id: '/repo/.claude/worktrees/lane-1', label: 'lane-1' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-2', label: 'lane-2' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-3', label: 'lane-3' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-4', label: 'lane-4' }),
      makeLane({ id: '/repo/.claude/worktrees/lane-5', label: 'lane-5' })
    ]

    render(
      <ConductorLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    // Rollup node header is visible
    expect(screen.getByTitle('5 lanes')).toBeTruthy()

    // Default collapsed: child lanes not yet in DOM
    expect(screen.queryByTitle('lane-1')).toBeNull()
    expect(screen.queryByTitle('lane-5')).toBeNull()

    // Click to expand
    fireEvent.click(screen.getByRole('button', { name: /5 lanes/ }))

    // Now all 5 lanes are rendered
    expect(screen.getByTitle(/lane-1/)).toBeTruthy()
    expect(screen.getByTitle(/lane-2/)).toBeTruthy()
    expect(screen.getByTitle(/lane-3/)).toBeTruthy()
    expect(screen.getByTitle(/lane-4/)).toBeTruthy()
    expect(screen.getByTitle(/lane-5/)).toBeTruthy()
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
      <ConductorLaneRollup
        lanes={lanes}
        renderRows={() => null}
        repoRoot="/repo"
      />
    )

    // Shows 5 lanes · 2 running
    expect(screen.getByTitle('5 lanes · 2 running')).toBeTruthy()

    // Has running dot and running arc
    expect(container.querySelector('[data-running-dot]')).toBeTruthy()
    expect(container.querySelector('[data-running-arc]')).toBeTruthy()
  })
})

describe('RepoFlatSection integration', () => {
  beforeEach(() => {
    mockDotStates.set({})
    $sidebarWorkspaceNodeOpen.set({})
  })

  it('5 conductor lanes + 1 regular lane -> one "5 lanes" node directly after home lane, default collapsed, expands to 5 lanes', () => {
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

    // Home lane and regular lane are directly visible
    expect(screen.getByTitle(/main/)).toBeTruthy()
    expect(screen.getByTitle(/feat-payment/)).toBeTruthy()

    // Conductor lanes are collapsed into "5 lanes" node
    expect(screen.getByTitle('5 lanes')).toBeTruthy()
    expect(screen.queryByTitle(/lane-1/)).toBeNull()
    expect(screen.queryByTitle(/lane-5/)).toBeNull()

    // Expanding "5 lanes" reveals the 5 conductor lanes
    fireEvent.click(screen.getByRole('button', { name: /5 lanes/ }))
    expect(screen.getByTitle(/lane-1/)).toBeTruthy()
    expect(screen.getByTitle(/lane-2/)).toBeTruthy()
    expect(screen.getByTitle(/lane-3/)).toBeTruthy()
    expect(screen.getByTitle(/lane-4/)).toBeTruthy()
    expect(screen.getByTitle(/lane-5/)).toBeTruthy()
  })

  it('zero conductor lanes -> no rollup node rendered', () => {
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
    expect(screen.getByTitle(/feat-login/)).toBeTruthy()
    expect(screen.queryByTitle(/lanes/i)).toBeNull()
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
    expect(screen.getByTitle('1 lane')).toBeTruthy()
  })
})
