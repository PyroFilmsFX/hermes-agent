import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type * as ConductorsStore from '@/store/conductors'
import type { SessionInfo } from '@/types/hermes'

vi.mock('@/store/conductors', async importOriginal => {
  const actual = await importOriginal<typeof ConductorsStore>()

  return {
    ...actual,
    acquireConductorsPoller: vi.fn(() => () => {}),
    refreshConductors: vi.fn(async () => null)
  }
})

const { $conductors } = await import('@/store/conductors')
const { $sessions } = await import('@/store/session')
const { ConductorsPane } = await import('./conductors-pane')
const { makeConductorRow, makeResponse } = await import('./conductors-fixtures')

const NOW_S = Math.floor(Date.now() / 1000)

function session(id: string, title: string, extra: Partial<SessionInfo> = {}): SessionInfo {
  return { id, title, last_active: NOW_S - 300, started_at: NOW_S - 600, ...extra } as SessionInfo
}

function setRows(...keys: string[]) {
  $conductors.set({
    status: 'ready',
    data: makeResponse(keys.map(key => makeConductorRow(key))),
    fetchedAt: Date.now(),
    failures: 0
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  $sessions.set([])
  setRows('a')
})

afterEach(() => {
  cleanup()
  $sessions.set([])
})

const toggle = () => document.querySelector('[data-slot="conductors-unbuilt-toggle"]') as HTMLElement | null
const items = () => [...document.querySelectorAll('[data-slot="conductors-unbuilt-item"]')]

describe('Conductors: orchestrators without a build (#49 O3)', () => {
  it('lists an orchestrator-role session with no row, with role badge and age', () => {
    $sessions.set([session('s-orch', 'infra-orchestrator')])
    render(<ConductorsPane />)
    fireEvent.click(toggle()!)

    expect(items()).toHaveLength(1)
    expect(items()[0].textContent).toContain('infra-orchestrator')
    expect(items()[0].textContent).toContain('orchestrator')
    expect(items()[0].textContent).toMatch(/\dm ago|\d+ m ago/)
  })

  it('lists a manager (explicit tag) too', () => {
    $sessions.set([session('s-m', 'Plain title', { session_role: 'manager' })])
    render(<ConductorsPane />)
    fireEvent.click(toggle()!)
    expect(items()).toHaveLength(1)
  })

  it('does not list a session that has a row', () => {
    $sessions.set([session('hs-a', 'a-orchestrator')])
    render(<ConductorsPane />)
    expect(toggle()).toBeNull()
  })

  it('does not list non-orchestrator sessions', () => {
    $sessions.set([session('w', 'thing-worker'), session('s', 'thing-stream'), session('p', 'plain')])
    render(<ConductorsPane />)
    expect(toggle()).toBeNull()
  })

  it('starts collapsed and expands on click', () => {
    $sessions.set([session('s-orch', 'infra-orchestrator')])
    render(<ConductorsPane />)

    expect(toggle()!.getAttribute('aria-expanded')).toBe('false')
    expect(items()).toHaveLength(0)
    fireEvent.click(toggle()!)
    expect(toggle()!.getAttribute('aria-expanded')).toBe('true')
    expect(items()).toHaveLength(1)
  })

  it('is hidden when there are none', () => {
    render(<ConductorsPane />)
    expect(toggle()).toBeNull()
  })

  it('opens the session through the open action', () => {
    const onOpenSession = vi.fn()
    $sessions.set([session('s-orch', 'infra-orchestrator')])
    render(<ConductorsPane onOpenSession={onOpenSession} />)
    fireEvent.click(toggle()!)
    fireEvent.click(screen.getByText('Open'))
    expect(onOpenSession).toHaveBeenCalledTimes(1)
    expect(onOpenSession.mock.calls[0][0]).toBe('s-orch')
  })

  it('appears when sessions arrive after mount', () => {
    render(
      <PaneVisibleContext.Provider value>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )
    expect(toggle()).toBeNull()
    act(() => $sessions.set([session('s-orch', 'infra-orchestrator')]))
    expect(toggle()).not.toBeNull()
  })
})
