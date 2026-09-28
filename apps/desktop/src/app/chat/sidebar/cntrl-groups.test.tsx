// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import type { HermesConnection } from '@/global'
import { readKey } from '@/lib/storage'
import { $pinnedSessionIds, $sidebarGroupFilter, setSidebarGroupFilter, setSidebarGrouping } from '@/store/layout'
import { $sessions, setConnection } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import {
  $cntrlGroupCollapsed,
  $cntrlGroups,
  $cntrlGroupsAvailable,
  type CntrlGroup,
  orderedCntrlGroups,
  refreshCntrlGroups,
  reorderCntrlGroups,
  toggleCntrlGroupCollapsed
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
  expect(order).toEqual(['Ops', 'Research', '__ungrouped__'])
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
