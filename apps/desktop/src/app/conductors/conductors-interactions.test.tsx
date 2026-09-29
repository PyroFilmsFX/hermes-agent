import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as OpenSessionModule from '@/app/open-session'
import type * as ConductorsStore from '@/store/conductors'

vi.mock('@/store/conductors', async importOriginal => {
  const actual = await importOriginal<typeof ConductorsStore>()

  return { ...actual, acquireConductorsPoller: vi.fn(() => () => {}), refreshConductors: vi.fn(async () => null) }
})

// Keep the real modifier → intent reader; spy on the one door that opens.
vi.mock('@/app/open-session', async importOriginal => {
  const actual = await importOriginal<typeof OpenSessionModule>()

  return { ...actual, openSession: vi.fn() }
})

const { $conductors, refreshConductors } = await import('@/store/conductors')
const { openSession } = await import('@/app/open-session')
const { $activeProfile } = await import('@/store/profile')
const { clearSessionDraft, takeSessionDraft } = await import('@/store/composer')
const { ConductorsPane } = await import('./conductors-pane')
const { makeConductorRow, makeResponse } = await import('./conductors-fixtures')

const open = vi.mocked(openSession)
const refresh = vi.mocked(refreshConductors)

const ROWS = () => [
  // Sorted by last activity within the same group: a, then b, then c.
  makeConductorRow('a', {
    build: {
      last_activity_at: 3,
      gates: [
        {
          id: 'g1',
          kind: 'ci',
          label: 'CI run',
          since: '2026-09-29T07:12:00Z',
          due: '2026-09-30T12:00:00Z',
          ref: null,
          state: 'waiting',
          waiter: true
        },
        {
          id: 'g2',
          kind: 'review',
          label: 'Cross-model review',
          since: '',
          due: null,
          ref: null,
          state: 'waiting',
          waiter: false
        }
      ],
      refusals: [{ seat: 'agy', code: 'stale_daemon', count: 2, last_at: '', note: 'daemon older than plugin' }],
      ci: [
        {
          kind: 'pr',
          ref: '',
          branch: 'b',
          pr: 123,
          state: 'success',
          url: 'https://github.com/o/r/pull/123',
          checked_at: ''
        }
      ]
    },
    other_builds: [
      { run_id: 'run-old', plan_title: 'Earlier plan', liveness: 'idle', waves: { done: 1, total: 3, current: 2 } }
    ]
  }),
  makeConductorRow('b', { build: { last_activity_at: 2 }, orchestrator: { profile: 'writer', title: 'Writer build' } }),
  makeConductorRow('c', { build: { last_activity_at: 1 }, orchestrator: { hermes_session_id: null, title: null } })
]

function renderPane() {
  $conductors.set({ status: 'ready', data: makeResponse(ROWS()), fetchedAt: Date.now(), failures: 0 })
  const view = render(<ConductorsPane />)
  const row = (key: string) => view.container.querySelector<HTMLElement>(`[data-row-key="${key}"]`)!

  return { ...view, row }
}

const writeText = vi.fn(async () => {})

beforeEach(() => {
  vi.clearAllMocks()
  $activeProfile.set('default')
  clearSessionDraft('hs-a')
  clearSessionDraft('hs-b')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
})

afterEach(() => {
  $activeProfile.set('default')
})

describe('open intents (§8)', () => {
  it('plain click stacks, ⌘-click opens a tab, ⇧⌘-click a window', () => {
    const { row } = renderPane()

    fireEvent.click(row('a'))
    fireEvent.click(row('a'), { metaKey: true })
    fireEvent.click(row('a'), { metaKey: true, shiftKey: true })
    fireEvent.click(row('a'), { ctrlKey: true })

    expect(open.mock.calls.map(call => [call[0], call[2], call[3]])).toEqual([
      ['hs-a', 'stack', undefined],
      ['hs-a', 'tab', undefined],
      ['hs-a', 'window', undefined],
      ['hs-a', 'tab', undefined]
    ])
  })

  it('a cross-profile row opens under its own profile (a tab, since main carries no owner)', () => {
    const { row } = renderPane()

    fireEvent.click(row('b'))
    fireEvent.click(row('b'), { metaKey: true, shiftKey: true })

    const scope = { ownerProfile: 'writer', workspaceMode: 'sessions' }
    expect(open.mock.calls.map(call => [call[0], call[2], call[3]])).toEqual([
      ['hs-b', 'tab', scope],
      ['hs-b', 'window', scope]
    ])
  })

  it('the same row is local once the window is on its profile', () => {
    $activeProfile.set('writer')
    const { row } = renderPane()
    fireEvent.click(row('b'))
    expect(open.mock.calls[0].slice(2)).toEqual(['stack', undefined])
  })

  it('an unattributed row never opens', () => {
    const { row } = renderPane()
    fireEvent.click(row('c'))
    act(() => row('c').focus())
    fireEvent.keyDown(row('c'), { key: 'Enter' })
    expect(open).not.toHaveBeenCalled()
    expect(row('c').getAttribute('aria-disabled')).toBe('true')
  })
})

describe('keyboard (roving tabindex)', () => {
  it('one tab stop; ↑/↓/Home/End move focus and the stop follows', () => {
    const { row } = renderPane()
    expect(['a', 'b', 'c'].map(key => row(key).tabIndex)).toEqual([0, -1, -1])

    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(row('b'))
    expect(['a', 'b', 'c'].map(key => row(key).tabIndex)).toEqual([-1, 0, -1])

    fireEvent.keyDown(row('b'), { key: 'End' })
    expect(document.activeElement).toBe(row('c'))
    fireEvent.keyDown(row('c'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(row('c'))

    fireEvent.keyDown(row('c'), { key: 'ArrowUp' })
    expect(document.activeElement).toBe(row('b'))
    fireEvent.keyDown(row('b'), { key: 'Home' })
    expect(document.activeElement).toBe(row('a'))
    expect(row('a').tabIndex).toBe(0)
  })

  it('Enter opens the focused row (stack), ⌘Enter as a tab', () => {
    const { row } = renderPane()
    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: 'Enter' })
    fireEvent.keyDown(row('a'), { key: 'Enter', metaKey: true })
    expect(open.mock.calls.map(call => call[2])).toEqual(['stack', 'tab'])
  })

  it('Space expands the row detail with gates, refusals and other builds, and collapses it again', () => {
    const { container, row } = renderPane()
    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: ' ' })

    const detail = container.querySelector('[data-row-detail="a"]')!
    expect(detail.getAttribute('role')).toBe('row')
    expect(row('a').getAttribute('aria-expanded')).toBe('true')
    expect(detail.querySelector('[data-detail="gates"]')?.textContent).toContain('CI · CI run')
    expect(detail.querySelector('[data-detail="gates"]')?.textContent).toContain('Review · Cross-model review')
    expect(detail.querySelector('[data-detail="refusals"]')?.textContent).toContain(
      'agy stale_daemon × 2 — daemon older than plugin'
    )
    expect(detail.querySelector('[data-detail="other-builds"]')?.textContent).toContain('Earlier plan · W2/3 · Idle')
    expect(open).not.toHaveBeenCalled()

    fireEvent.keyDown(row('a'), { key: ' ' })
    expect(container.querySelector('[data-row-detail="a"]')).toBeNull()
    expect(row('a').getAttribute('aria-expanded')).toBe('false')
  })

  it('the chevron toggles the detail without opening the session; unattributed rows expand too', () => {
    const { container, row } = renderPane()
    fireEvent.click(row('c').querySelector('[data-slot="conductor-row-expand"]')!)
    expect(container.querySelector('[data-row-detail="c"]')).toBeTruthy()
    fireEvent.click(row('a').querySelector('[data-slot="conductor-row-expand"]')!)
    expect(container.querySelector('[data-row-detail="a"]')).toBeTruthy()
    expect(open).not.toHaveBeenCalled()
  })

  it('⌘C copies the run id', () => {
    const { row } = renderPane()
    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: 'c', metaKey: true })
    expect(writeText).toHaveBeenCalledWith('run-a')
  })
})

describe('Send… (§8)', () => {
  it('S opens "Message to <session>"; Enter stashes the draft and opens the session as a tab — nothing is sent', async () => {
    const { row } = renderPane()
    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: 's' })

    const field = await screen.findByLabelText('Message to Session a')
    fireEvent.change(field, { target: { value: 'Ship wave 3 after CI' } })
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(takeSessionDraft('hs-a').text).toBe('Ship wave 3 after CI')
    expect(open).toHaveBeenCalledTimes(1)
    expect(open.mock.calls[0][0]).toBe('hs-a')
    expect(open.mock.calls[0][2]).toBe('tab')
    // The popover closed; the row itself never "opened" from the Enter.
    expect(screen.queryByLabelText('Message to Session a')).toBeNull()
  })

  it('appends to a draft already in that composer instead of replacing it', async () => {
    const { stashSessionDraft } = await import('@/store/composer')
    stashSessionDraft('hs-a', 'half-written note', [])

    const { row } = renderPane()
    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: 'S' })
    const field = await screen.findByLabelText('Message to Session a')
    fireEvent.change(field, { target: { value: 'and this' } })
    fireEvent.submit(field.closest('form')!)

    expect(takeSessionDraft('hs-a').text).toBe('half-written note\n\nand this')
  })

  it('Shift+Enter is a newline and a blank message does nothing', async () => {
    const { row } = renderPane()
    act(() => row('a').focus())
    fireEvent.keyDown(row('a'), { key: 's' })
    const field = await screen.findByLabelText('Message to Session a')
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(open).not.toHaveBeenCalled()
    expect(takeSessionDraft('hs-a').text).toBe('')
  })

  it('a cross-profile Send opens under that profile', async () => {
    const { row } = renderPane()
    act(() => row('b').focus())
    fireEvent.keyDown(row('b'), { key: 's' })
    const field = await screen.findByLabelText('Message to Writer build')
    fireEvent.change(field, { target: { value: 'hello' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(open.mock.calls[0].slice(2)).toEqual(['tab', { ownerProfile: 'writer', workspaceMode: 'sessions' }])
    expect(takeSessionDraft('hs-b').text).toBe('hello')
  })

  it('an unattributed row has no Send…', () => {
    const { row } = renderPane()
    act(() => row('c').focus())
    fireEvent.keyDown(row('c'), { key: 's' })
    expect(screen.queryByLabelText(/Message to/)).toBeNull()
  })
})

describe('overflow menu', () => {
  const openMenu = async (el: HTMLElement) => {
    act(() => el.focus())
    fireEvent.keyDown(el, { key: 'F10', shiftKey: true })

    return screen.findByRole('menu')
  }

  it('Open, Copy session id, Copy run id, Open PR and Refresh', async () => {
    const { row } = renderPane()
    const menu = await openMenu(row('a'))
    const labels = [...menu.querySelectorAll('[role="menuitem"]')].map(item => item.textContent)
    expect(labels).toEqual(['Open session', 'Send…', 'Copy session id', 'Copy run id', 'Open PR #123', 'Refresh'])

    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy session id' }))
    expect(writeText).toHaveBeenCalledWith('hs-a')
    expect(open).not.toHaveBeenCalled()
  })

  it('Refresh forces a fresh read', async () => {
    const { row } = renderPane()
    await openMenu(row('a'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Refresh' }))
    expect(refresh).toHaveBeenCalledWith({ fresh: true })
  })

  it('Open session uses the plain-click intent', async () => {
    const { row } = renderPane()
    await openMenu(row('a'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Open session' }))
    expect(open).toHaveBeenCalledTimes(1)
    expect(open.mock.calls[0].slice(2)).toEqual(['stack', undefined])
  })

  it('an unattributed row still offers Copy run id, but not Open or Send…', async () => {
    const { row } = renderPane()
    const menu = await openMenu(row('c'))
    const labels = [...menu.querySelectorAll('[role="menuitem"]')].map(item => item.textContent)
    expect(labels).toEqual(['Copy run id', 'Refresh'])
  })
})
