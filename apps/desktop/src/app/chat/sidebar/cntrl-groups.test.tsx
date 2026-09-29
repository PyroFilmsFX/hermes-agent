// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import type { HermesConnection } from '@/global'
import { readKey } from '@/lib/storage'
import {
  $pinnedSessionIds,
  $sidebarFiltersActive,
  $sidebarGroupFilter,
  $sidebarStatusFilter,
  setSidebarGroupFilter,
  setSidebarGrouping
} from '@/store/layout'
import { $sessions, setConnection } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import {
  $cntrlGroupCollapsed,
  $cntrlGroups,
  $cntrlGroupsAvailable,
  CNTRL_GROUP_UNGROUPED,
  type CntrlGroup,
  clearCntrlGroup,
  migrateCntrlGroup,
  orderedCntrlGroups,
  refreshCntrlGroups,
  reorderCntrlGroups,
  toggleCntrlGroupCollapsed,
  ungroupAllCntrlGroup,
  updateCntrlGroup
} from './cntrl-groups'

import { ChatSidebar } from './index'

// The plugin API is the only seam: `null` stands for "plugin absent" (the
// dashboard 404s /api/plugins/cntrl_groups/* when it is disabled or missing).
const api = vi.hoisted(() => ({ groups: null as CntrlGroup[] | null, calls: [] as unknown[][] }))

vi.mock('@/api/plugins', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pluginRest: vi.fn(async (pluginId: string, path: string, opts?: { method?: string; body?: unknown }) => {
    api.calls.push([pluginId, path, opts?.method ?? 'GET', opts?.body])

    if (pluginId !== 'cntrl_groups' || api.groups === null) {
      throw new Error('Plugin not found')
    }

    return path === '/groups' && !opts?.method ? api.groups : {}
  })
}))

const noop = () => {}
const noopAsync = async () => {}

const mount = () =>
  render(
    <MemoryRouter>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={noopAsync}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

const now = () => Date.now() / 1000

const openTriggerMenu = (trigger: HTMLElement) => {
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

beforeEach(() => {
  api.groups = null
  api.calls = []
})

afterEach(() => {
  cleanup()
  setConnection({ baseUrl: 'http://127.0.0.1:8000', mode: 'local', profile: 'default' } as HermesConnection)
  setSidebarGroupFilter(null)
  setSidebarGrouping('date')
  $pinnedSessionIds.set([])
  $sidebarStatusFilter.set([])
  $cntrlGroupCollapsed.set([])
  $cntrlGroups.set([])
  $cntrlGroupsAvailable.set(null)
  $sessions.set([])
})

it('draws groups like gateway groups, Ungrouped last, and keeps a pinned grouped session only in Pinned', async () => {
  api.groups = [
    { name: 'Research', session_ids: ['grouped', 'pinned-grouped'], pinned: false, order: 0 },
    { name: 'Ops', session_ids: ['ops'], pinned: true, order: 0 }
  ]
  mount()
  act(() => {
    setSidebarGrouping('groups')
    $pinnedSessionIds.set(['pinned-grouped'])
    $sessions.set([
      makeSessionInfo({ id: 'grouped', title: 'Grouped chat', last_active: now() }),
      makeSessionInfo({ id: 'pinned-grouped', title: 'Pinned grouped chat', last_active: now() }),
      makeSessionInfo({ id: 'ops', title: 'Ops chat', last_active: now() }),
      makeSessionInfo({ id: 'loose', title: 'Loose chat', last_active: now() })
    ])
  })

  await waitFor(() => expect(screen.getByText('Research')).toBeTruthy())

  const order = [...document.querySelectorAll('[data-cntrl-group]')].map(node => node.getAttribute('data-cntrl-group'))
  expect(order).toEqual(['Ops', 'Research', '/ungrouped'])
  // Pins stay global: one row, in Pinned, never repeated under its group.
  expect(screen.getAllByText('Pinned grouped chat')).toHaveLength(1)

  const research = document.querySelector('[data-cntrl-group="Research"]') as HTMLElement
  expect(within(research).queryByText('Pinned grouped chat')).toBeNull()
  fireEvent.click(within(research).getByRole('button', { name: 'Hide Research sessions' }))
  expect(screen.queryByText('Grouped chat')).toBeNull()
  expect($cntrlGroupCollapsed.get()).toContain('Research')
  fireEvent.click(within(research).getByRole('button', { name: 'Show Research sessions' }))
  expect(screen.getByText('Grouped chat')).toBeTruthy()
  expect(within(research).getByRole('button', { name: 'Group actions: Research' })).toBeTruthy()
})

it('degrades to the dated list, with no group filter, when the plugin API is absent', async () => {
  api.groups = null
  setSidebarGroupFilter('Research')
  mount()
  act(() => {
    setSidebarGrouping('groups')
    $sessions.set([makeSessionInfo({ id: 'loose', title: 'Loose chat', last_active: now() })])
  })

  await waitFor(() => expect($cntrlGroupsAvailable.get()).toBe(false))
  // A remembered filter must not hide everything behind a plugin that is gone.
  expect(screen.getByText('Loose chat')).toBeTruthy()
  expect(document.querySelector('[data-cntrl-group]')).toBeNull()
})

it('marks the plugin available only when the groups read answers', async () => {
  api.groups = null
  await refreshCntrlGroups()
  expect($cntrlGroupsAvailable.get()).toBe(false)
  expect($cntrlGroups.get()).toEqual([])

  api.groups = [{ name: 'Research', session_ids: ['a'], pinned: false, order: 0 }]
  await refreshCntrlGroups()
  expect($cntrlGroupsAvailable.get()).toBe(true)
  expect($cntrlGroups.get()).toHaveLength(1)
})

it('renumbers a band on reorder so fresh (all order 0) groups actually move', async () => {
  api.groups = []
  const band: CntrlGroup[] = [
    { name: 'A', session_ids: ['a'], pinned: false, order: 0 },
    { name: 'B', session_ids: ['b'], pinned: false, order: 0 }
  ]

  await reorderCntrlGroups(['B', 'A'], band)
  const patches = api.calls.filter(call => call[2] === 'PATCH')
  expect(patches).toEqual([['cntrl_groups', '/groups/A', 'PATCH', { order: 1 }]])
})

it('sorts pinned groups first and keeps their explicit order', () => {
  expect(
    orderedCntrlGroups([
      { name: 'Later', session_ids: [], pinned: false, order: 1 },
      { name: 'Pinned second', session_ids: [], pinned: true, order: 2 },
      { name: 'Pinned first', session_ids: [], pinned: true, order: 1 },
      { name: 'Earlier', session_ids: [], pinned: false, order: 0 }
    ]).map(group => group.name)
  ).toEqual(['Pinned first', 'Pinned second', 'Earlier', 'Later'])
})

it('persists the selected group filter in its connection scope', () => {
  const connectionA = { baseUrl: 'https://groups-a.example', mode: 'remote', profile: 'default' } as HermesConnection
  const connectionB = { baseUrl: 'https://groups-b.example', mode: 'remote', profile: 'default' } as HermesConnection
  setConnection(connectionA)
  setSidebarGroupFilter('Research')
  const keyA = 'hermes.desktop.sidebarGroupFilter.v1.remote.https%3A%2F%2Fgroups-a.example.default'
  expect(readKey(keyA)).toBe(JSON.stringify('Research'))

  act(() => setConnection(connectionB))
  expect($sidebarGroupFilter.get()).toBeNull()
  setSidebarGroupFilter('Unsorted')
  expect(readKey(keyA)).toBe(JSON.stringify('Research'))
})

it('toggles a group collapsed preference back open', () => {
  toggleCntrlGroupCollapsed('Research')
  expect($cntrlGroupCollapsed.get()).toEqual(['Research'])
  toggleCntrlGroupCollapsed('Research')
  expect($cntrlGroupCollapsed.get()).toEqual([])
})

it('uses /ungrouped as the sentinel for ungrouped sessions, allowing a real group named __ungrouped__ without collision', async () => {
  api.groups = [
    { name: '__ungrouped__', session_ids: ['grouped-in-literal-ungrouped'], pinned: false, order: 0 }
  ]
  mount()
  act(() => {
    setSidebarGrouping('groups')
    $sessions.set([
      makeSessionInfo({ id: 'grouped-in-literal-ungrouped', title: 'Named group chat', last_active: now() }),
      makeSessionInfo({ id: 'loose', title: 'Loose chat', last_active: now() })
    ])
  })

  await waitFor(() => expect(screen.getByText('Named group chat')).toBeTruthy())

  const sections = [...document.querySelectorAll('[data-cntrl-group]')].map(node => node.getAttribute('data-cntrl-group'))
  expect(sections).toEqual(['__ungrouped__', CNTRL_GROUP_UNGROUPED])

  // Filter by the real group named '__ungrouped__' -> keeps only sessions in that group
  act(() => setSidebarGroupFilter('__ungrouped__'))
  expect(screen.getByText('Named group chat')).toBeTruthy()
  expect(screen.queryByText('Loose chat')).toBeNull()

  // Filter by CNTRL_GROUP_UNGROUPED -> keeps only ungrouped/loose sessions
  act(() => setSidebarGroupFilter(CNTRL_GROUP_UNGROUPED))
  expect(screen.queryByText('Named group chat')).toBeNull()
  expect(screen.getByText('Loose chat')).toBeTruthy()

  // Collapsing the ungrouped section persists CNTRL_GROUP_UNGROUPED
  const ungroupedSection = document.querySelector(`[data-cntrl-group="${CNTRL_GROUP_UNGROUPED}"]`) as HTMLElement
  fireEvent.click(within(ungroupedSection).getByRole('button', { name: 'Hide Ungrouped sessions' }))
  expect($cntrlGroupCollapsed.get()).toContain(CNTRL_GROUP_UNGROUPED)
  expect($cntrlGroupCollapsed.get()).not.toContain('__ungrouped__')
})

it('migrates a persisted group filter and collapsed entry when a group is renamed', async () => {
  api.groups = [{ name: 'OldName', session_ids: ['s1'], pinned: false, order: 0 }]
  setSidebarGroupFilter('OldName')
  $cntrlGroupCollapsed.set(['OldName', 'OtherGroup'])

  await updateCntrlGroup('OldName', { name: 'NewName' })

  expect($sidebarGroupFilter.get()).toBe('NewName')
  expect($cntrlGroupCollapsed.get()).toEqual(['NewName', 'OtherGroup'])
})

it('clears a persisted group filter and collapsed entry when ungrouping all sessions in that group', async () => {
  api.groups = [{ name: 'ToClear', session_ids: ['s1', 's2'], pinned: false, order: 0 }]
  setSidebarGroupFilter('ToClear')
  $cntrlGroupCollapsed.set(['ToClear', 'OtherGroup'])

  await ungroupAllCntrlGroup('ToClear', ['s1', 's2'])

  expect($sidebarGroupFilter.get()).toBeNull()
  expect($cntrlGroupCollapsed.get()).toEqual(['OtherGroup'])
  const deletes = api.calls.filter(call => call[2] === 'DELETE')
  expect(deletes).toHaveLength(2)
})

it('migrates filter and collapsed state through the group header rename and ungroup-all menus', async () => {
  api.groups = [{ name: 'Research', session_ids: ['s1'], pinned: false, order: 0 }]
  setSidebarGroupFilter('Research')
  $cntrlGroupCollapsed.set(['Research'])
  mount()
  act(() => {
    setSidebarGrouping('groups')
    $sessions.set([makeSessionInfo({ id: 's1', title: 'Research session', last_active: now() })])
  })

  await waitFor(() => expect(screen.getByText('Research')).toBeTruthy())

  // Open actions menu and click Rename group
  const research = document.querySelector('[data-cntrl-group="Research"]') as HTMLElement
  openTriggerMenu(within(research).getByRole('button', { name: 'Group actions: Research' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Rename group' }))

  // Type new name in the dialog and submit
  const input = screen.getByLabelText('Group name')
  fireEvent.change(input, { target: { value: 'Investigations' } })
  api.groups = [{ name: 'Investigations', session_ids: ['s1'], pinned: false, order: 0 }]
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect($sidebarGroupFilter.get()).toBe('Investigations'))
  expect($cntrlGroupCollapsed.get()).toEqual(['Investigations'])

  // Now test Ungroup all on Investigations
  await waitFor(() => expect(screen.getByText('Investigations')).toBeTruthy())
  const investigations = document.querySelector('[data-cntrl-group="Investigations"]') as HTMLElement
  openTriggerMenu(within(investigations).getByRole('button', { name: 'Group actions: Investigations' }))
  api.groups = [{ name: 'Investigations', session_ids: [], pinned: false, order: 0 }]
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Ungroup all' }))

  await waitFor(() => expect($sidebarGroupFilter.get()).toBeNull())
  expect($cntrlGroupCollapsed.get()).toEqual([])
})

it('excludes a persisted group filter from $sidebarFiltersActive when $cntrlGroupsAvailable is false', () => {
  setSidebarGroupFilter('Research')
  $cntrlGroupsAvailable.set(null)
  expect($sidebarFiltersActive.get()).toBe(true)

  $cntrlGroupsAvailable.set(true)
  expect($sidebarFiltersActive.get()).toBe(true)

  $cntrlGroupsAvailable.set(false)
  expect($sidebarFiltersActive.get()).toBe(false)

  // Another active filter still activates $sidebarFiltersActive even when groups plugin is absent
  $sidebarStatusFilter.set(['working'])
  expect($sidebarFiltersActive.get()).toBe(true)
  $sidebarStatusFilter.set([])
  expect($sidebarFiltersActive.get()).toBe(false)
})

it('does not show the filter button as active when only a group filter is persisted and the plugin is absent', async () => {
  api.groups = null
  setSidebarGroupFilter('Research')
  mount()

  await waitFor(() => expect($cntrlGroupsAvailable.get()).toBe(false))
  expect($sidebarFiltersActive.get()).toBe(false)

  const filterBtn = screen.getByRole('button', { name: 'Filters' })
  expect(filterBtn.classList.contains('bg-(--ui-control-active-background)')).toBe(false)
})
