import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type { ConductorsResponse } from '@/api/conductors'

vi.mock('@/api/conductors', async importOriginal => {
  const actual = await importOriginal<typeof import('@/api/conductors')>()
  return {
    ...actual,
    fetchConductors: vi.fn()
  }
})

const { fetchConductors } = await import('@/api/conductors')
const {
  $conductors,
  $conductorsNow,
  _resetConductorsPollerForTest,
  FOCUSED_POLL_INTERVAL_MS,
  RELATIVE_TIME_TICK_MS
} = await import('@/store/conductors')
const { ConductorsPane } = await import('./conductors-pane')
const { conductorRowRenderProbe } = await import('./conductor-row')
const { makeConductorRow, makeResponse } = await import('./conductors-fixtures')

const mockedFetchConductors = vi.mocked(fetchConductors)

describe('Conductors performance acceptance (#49 T2)', () => {
  let hasFocusSpy: ReturnType<typeof vi.spyOn>
  let originalVisibilityState: PropertyDescriptor | undefined

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    _resetConductorsPollerForTest()

    originalVisibilityState = Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState')
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true
    })

    hasFocusSpy = vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    conductorRowRenderProbe.current = null
  })

  afterEach(() => {
    conductorRowRenderProbe.current = null
    _resetConductorsPollerForTest()
    vi.clearAllTimers()
    vi.useRealTimers()
    hasFocusSpy.mockRestore()

    if (originalVisibilityState) {
      Object.defineProperty(Document.prototype, 'visibilityState', originalVisibilityState)
    }
  })

  it('with the pane hidden no fetch and no timers run', async () => {
    const response = makeResponse([makeConductorRow('r1')])
    mockedFetchConductors.mockResolvedValue(response)

    // 1. Mount while hidden (inactive tab)
    const { rerender } = render(
      <PaneVisibleContext.Provider value={false}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    expect(mockedFetchConductors).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)

    // Advance time while hidden: no poll fires, no timers scheduled
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS * 4)
    })
    expect(mockedFetchConductors).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)

    // 2. Show the pane: poller and ticker arm, fetch occurs
    rerender(
      <PaneVisibleContext.Provider value={true}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    expect(mockedFetchConductors).toHaveBeenCalledTimes(1)
    await act(async () => {
      await Promise.resolve()
    })
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    // 3. Hide the pane again: timers must clear and polling ceases
    rerender(
      <PaneVisibleContext.Provider value={false}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    expect(vi.getTimerCount()).toBe(0)
    const callsBefore = mockedFetchConductors.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS * 4)
    })
    expect(mockedFetchConductors).toHaveBeenCalledTimes(callsBefore)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('a poll that returns identical data re-renders zero rows', async () => {
    const rowA = makeConductorRow('row-a')
    const rowB = makeConductorRow('row-b')
    const rowC = makeConductorRow('row-c')
    const initialData = makeResponse([rowA, rowB, rowC])

    mockedFetchConductors.mockResolvedValue(initialData)

    const renderedKeys: string[] = []
    conductorRowRenderProbe.current = (key: string) => {
      renderedKeys.push(key)
    }

    render(
      <PaneVisibleContext.Provider value={true}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    // Initial mount and fetch renders all 3 rows
    await act(async () => {
      await Promise.resolve()
    })
    expect(renderedKeys).toContain('row-a')
    expect(renderedKeys).toContain('row-b')
    expect(renderedKeys).toContain('row-c')

    // Reset probe tracker
    renderedKeys.length = 0

    // Next poll returns fresh objects with identical structural content
    const identicalData = makeResponse([
      makeConductorRow('row-a'),
      makeConductorRow('row-b'),
      makeConductorRow('row-c')
    ])
    mockedFetchConductors.mockResolvedValue(identicalData)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    })

    // Reconcile by key keeps previous row instances, memoised ConductorRow skips render
    expect(renderedKeys).toEqual([])
  })

  it('one changed row re-renders only that row', async () => {
    const rowA = makeConductorRow('row-a')
    const rowB = makeConductorRow('row-b')
    const rowC = makeConductorRow('row-c')
    const initialData = makeResponse([rowA, rowB, rowC])

    mockedFetchConductors.mockResolvedValue(initialData)

    const renderedKeys: string[] = []
    conductorRowRenderProbe.current = (key: string) => {
      renderedKeys.push(key)
    }

    render(
      <PaneVisibleContext.Provider value={true}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    await act(async () => {
      await Promise.resolve()
    })
    renderedKeys.length = 0

    // Poll where only row-b has mutated data (e.g. waves.done incremented)
    const mutatedRowB = makeConductorRow('row-b', {
      build: { waves: { done: 2, total: 4, current: 3 } }
    })
    const partiallyChangedData = makeResponse([
      makeConductorRow('row-a'),
      mutatedRowB,
      makeConductorRow('row-c')
    ])
    mockedFetchConductors.mockResolvedValue(partiallyChangedData)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    })

    // Only row-b should re-render; row-a and row-c must not re-render
    expect(renderedKeys).toEqual(['row-b'])
  })

  it('only one interval exists for relative times regardless of row count', async () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval')

    // 50 rows in the response
    const fiftyRows = Array.from({ length: 50 }, (_, i) => makeConductorRow(`row-${i}`))
    const response50 = makeResponse(fiftyRows)
    mockedFetchConductors.mockResolvedValue(response50)

    render(
      <PaneVisibleContext.Provider value={true}>
        <ConductorsPane />
      </PaneVisibleContext.Provider>
    )

    await act(async () => {
      await Promise.resolve()
    })

    // Find all intervals registered with RELATIVE_TIME_TICK_MS (30 s)
    const relativeTimeIntervals = setIntervalSpy.mock.calls.filter(
      call => call[1] === RELATIVE_TIME_TICK_MS
    )

    // Exactly one shared relative-time interval exists, not 50 intervals
    expect(relativeTimeIntervals).toHaveLength(1)

    // None of the individual 50 rows registered their own intervals
    const allIntervalDelays = setIntervalSpy.mock.calls.map(call => call[1])
    // The only intervals scheduled in the entire store/pane are the poller and the single 30s ticker
    expect(allIntervalDelays).toEqual([RELATIVE_TIME_TICK_MS, FOCUSED_POLL_INTERVAL_MS])
  })
})
