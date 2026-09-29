import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type * as ConductorsStore from '@/store/conductors'

const release = vi.fn()

vi.mock('@/store/conductors', async importOriginal => {
  const actual = await importOriginal<typeof ConductorsStore>()

  return {
    ...actual,
    acquireConductorsPoller: vi.fn(() => release),
    refreshConductors: vi.fn(async () => null)
  }
})

const { $conductors, acquireConductorsPoller, refreshConductors } = await import('@/store/conductors')
const { ConductorsPane } = await import('./conductors-pane')
const { conductorRowRenderProbe } = await import('./conductor-row')
const { GENERATED_AT, makeConductorRow, makeResponse } = await import('./conductors-fixtures')
const { clockOf } = await import('./conductors-model')

const acquire = vi.mocked(acquireConductorsPoller)
const refresh = vi.mocked(refreshConductors)

function setState(state: Partial<ReturnType<typeof $conductors.get>>) {
  $conductors.set({ status: 'idle', data: null, fetchedAt: null, failures: 0, ...state })
}

beforeEach(() => {
  vi.clearAllMocks()
  setState({})
})

afterEach(() => {
  conductorRowRenderProbe.current = null
})

describe('poller lifecycle', () => {
  it('acquires on mount and releases on unmount', () => {
    const { unmount } = render(<ConductorsPane />)
    expect(acquire).toHaveBeenCalledTimes(1)
    expect(release).not.toHaveBeenCalled()
    unmount()
    expect(release).toHaveBeenCalledTimes(1)
  })

  it('releases while hidden (inactive keep-alive tab) and re-acquires when shown', () => {
    const view = (visible: boolean) => (
      <PaneVisibleContext.Provider value={visible}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    const { rerender } = render(view(true))
    expect(acquire).toHaveBeenCalledTimes(1)
    rerender(view(false))
    expect(release).toHaveBeenCalledTimes(1)
    rerender(view(true))
    expect(acquire).toHaveBeenCalledTimes(2)
  })

  it('never acquires while mounted hidden', () => {
    render(
      <PaneVisibleContext.Provider value={false}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )
    expect(acquire).not.toHaveBeenCalled()
  })
})

describe('states', () => {
  it('loading: three static skeleton rows on the first fetch', () => {
    setState({ status: 'loading' })
    const { container } = render(<ConductorsPane />)
    const skeletons = container.querySelectorAll('[data-slot="conductors-skeleton-row"]')
    expect(skeletons).toHaveLength(3)
    expect(container.querySelector('.animate-pulse')).toBeNull()
  })

  it('empty: copy, plus the missing-index line only when the index is missing', () => {
    setState({ status: 'ready', data: makeResponse([]), fetchedAt: Date.now() })
    const { unmount } = render(<ConductorsPane />)
    expect(screen.getByText('No conductor builds are running.')).toBeTruthy()
    expect(screen.getByText('Builds show here when a session arms tb-build.')).toBeTruthy()
    expect(screen.queryByText('No marker index at ~/.claude/state yet.')).toBeNull()
    unmount()

    setState({
      status: 'ready',
      data: makeResponse([], { sources: { marker_index: 'missing', state_dirs: 0, skipped: 0 } }),
      fetchedAt: Date.now()
    })
    render(<ConductorsPane />)
    expect(screen.getByText('No marker index at ~/.claude/state yet.')).toBeTruthy()
  })

  it('error: first fetch failed shows a message and Retry forces a fresh read', () => {
    setState({ status: 'error', failures: 1 })
    render(<ConductorsPane />)
    expect(screen.getByRole('alert').textContent).toContain('Couldn’t load conductors')
    refresh.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(refresh).toHaveBeenCalledWith({ fresh: true })
  })

  it('stale data: keeps rows and shows an amber "as of" banner', () => {
    const fetchedAt = Date.now() - 180_000
    setState({ status: 'error', data: makeResponse([makeConductorRow('a')]), fetchedAt, failures: 2 })
    const { container } = render(<ConductorsPane />)
    const banner = container.querySelector('[data-slot="conductors-stale-banner"]')
    expect(banner?.textContent).toContain(`Backend unreachable · showing data as of ${clockOf(fetchedAt)}`)
    expect(container.querySelector('[data-row-key="a"]')).toBeTruthy()
    refresh.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(refresh).toHaveBeenCalledWith({ fresh: true })
  })

  it('partial: ≈ marks appear only on derived columns', () => {
    const derived = makeConductorRow('d', { field_sources: { progress: 'derived', estimate: 'derived' } })
    setState({ status: 'ready', data: makeResponse([derived, makeConductorRow('s')]), fetchedAt: Date.now() })
    const { container } = render(<ConductorsPane />)
    const d = container.querySelector('[data-row-key="d"]')!
    const s = container.querySelector('[data-row-key="s"]')!
    expect(d.querySelectorAll('[data-col="now"] [data-derived]')).toHaveLength(1)
    expect(d.querySelectorAll('[data-col="remaining"] [data-derived]')).toHaveLength(1)
    expect(d.querySelectorAll('[data-col="estimate"] [data-derived]')).toHaveLength(0)
    expect(s.querySelectorAll('[data-derived]')).toHaveLength(0)
  })

  it('abandoned rows stay hidden until "Show abandoned (n)" is toggled', () => {
    const gone = makeConductorRow('gone', {
      orchestrator: { live: 'none' },
      build: { liveness: 'stale', idle_since: GENERATED_AT - 8 * 86_400 }
    })

    setState({ status: 'ready', data: makeResponse([makeConductorRow('live'), gone]), fetchedAt: Date.now() })
    const { container } = render(<ConductorsPane />)
    expect(container.querySelector('[data-row-key="gone"]')).toBeNull()
    expect(container.querySelector('[data-row-key="live"]')).toBeTruthy()

    const toggle = screen.getByRole('button', { name: 'Show abandoned (1)' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    const row = container.querySelector('[data-row-key="gone"]')
    expect(row).toBeTruthy()
    expect(row?.querySelector('[data-liveness]')?.textContent).toBe('Abandoned')
    expect(screen.getByRole('button', { name: 'Hide abandoned (1)' })).toBeTruthy()
  })

  it('an all-abandoned roster is not "empty": the toggle still reaches the rows', () => {
    const gone = makeConductorRow('gone', {
      orchestrator: { live: 'none' },
      build: { liveness: 'idle', idle_since: GENERATED_AT - 9 * 86_400 }
    })

    setState({ status: 'ready', data: makeResponse([gone]), fetchedAt: Date.now() })
    render(<ConductorsPane />)
    expect(screen.getByRole('button', { name: 'Show abandoned (1)' })).toBeTruthy()
  })
})

describe('rendering budget', () => {
  it('re-renders only the row whose data changed', () => {
    const counts = new Map<string, number>()
    conductorRowRenderProbe.current = key => counts.set(key, (counts.get(key) ?? 0) + 1)

    const rows = [makeConductorRow('a'), makeConductorRow('b'), makeConductorRow('c')]
    setState({ status: 'ready', data: makeResponse(rows), fetchedAt: Date.now() })
    render(<ConductorsPane />)
    const before = new Map(counts)

    // A fresh poll: every object is new, only row b's content differs.
    const next = JSON.parse(JSON.stringify(rows)) as typeof rows
    next[1] = { ...next[1], build: { ...next[1].build, lanes: { running: 5, stale: 0, cap: 6 } } }
    act(() => setState({ status: 'ready', data: makeResponse(next), fetchedAt: Date.now() }))

    expect(counts.get('a')).toBe(before.get('a'))
    expect(counts.get('c')).toBe(before.get('c'))
    expect(counts.get('b')).toBe((before.get('b') ?? 0) + 1)
  })
})

describe('grid a11y and open placeholder', () => {
  it('exposes a labelled grid with a header row and data rows', () => {
    setState({ status: 'ready', data: makeResponse([makeConductorRow('a')]), fetchedAt: Date.now() })
    render(<ConductorsPane />)
    const grid = screen.getByRole('grid', { name: 'Conductor builds' })
    expect(grid.querySelectorAll('[role="columnheader"]')).toHaveLength(10)
    expect(grid.querySelectorAll('[role="row"][data-row-key]')).toHaveLength(1)
  })

  it('opens attributed rows on click and leaves unattributed rows inert', () => {
    const onOpenRow = vi.fn()
    const bare = makeConductorRow('bare', { orchestrator: { hermes_session_id: null, title: null } })
    setState({ status: 'ready', data: makeResponse([makeConductorRow('a'), bare]), fetchedAt: Date.now() })
    const { container } = render(<ConductorsPane onOpenRow={onOpenRow} />)

    fireEvent.click(container.querySelector('[data-row-key="a"]')!)
    expect(onOpenRow).toHaveBeenCalledTimes(1)
    expect(onOpenRow.mock.calls[0][0].key).toBe('a')

    const inert = container.querySelector('[data-row-key="bare"]')!
    expect(inert.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(inert)
    expect(onOpenRow).toHaveBeenCalledTimes(1)
  })
})
