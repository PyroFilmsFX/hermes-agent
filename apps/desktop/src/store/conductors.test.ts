import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
  acquireConductorsPoller,
  refreshConductors,
  _resetConductorsPollerForTest,
  FOCUSED_POLL_INTERVAL_MS,
  BLURRED_POLL_INTERVAL_MS,
  MAX_CONSECUTIVE_FAILURES
} = await import('./conductors')

const mockedFetchConductors = vi.mocked(fetchConductors)

const sampleResponse: ConductorsResponse = {
  schema: 'hermes-conductors/v1',
  generated_at: 1790670000.0,
  rows: [],
  sources: {
    marker_index: 'ok',
    state_dirs: 1,
    skipped: 0
  },
  abandoned: 0
}

describe('conductors store and poller', () => {
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
    mockedFetchConductors.mockResolvedValue(sampleResponse)
  })

  afterEach(() => {
    _resetConductorsPollerForTest()
    vi.clearAllTimers()
    vi.useRealTimers()
    hasFocusSpy.mockRestore()

    if (originalVisibilityState) {
      Object.defineProperty(Document.prototype, 'visibilityState', originalVisibilityState)
    }
  })

  it('poller starts on first acquire and stops on last release', async () => {
    const release1 = acquireConductorsPoller()
    expect(mockedFetchConductors).toHaveBeenCalledTimes(1)

    // Second acquire does not trigger a duplicate poll
    const release2 = acquireConductorsPoller()
    expect(mockedFetchConductors).toHaveBeenCalledTimes(1)

    // Release 1 leaves one active lease, so polling continues
    release1()
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(2)

    // Last release stops the poller completely
    release2()
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS * 3)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(2)
  })

  it('hidden document → no fetch', async () => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true
    })

    const release = acquireConductorsPoller()
    expect(mockedFetchConductors).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS * 4)
    expect(mockedFetchConductors).not.toHaveBeenCalled()

    // When document becomes visible, polling triggers immediately
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true
    })
    document.dispatchEvent(new Event('visibilitychange'))
    expect(mockedFetchConductors).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(2)

    release()
  })

  it('focused 15 s / blurred 60 s cadence', async () => {
    const release = acquireConductorsPoller()
    expect(mockedFetchConductors).toHaveBeenCalledTimes(1)

    // Advance 15s in focused state
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(3)

    // Switch to blurred state
    hasFocusSpy.mockReturnValue(false)
    window.dispatchEvent(new Event('blur'))

    // At 15s blurred, no fetch should run
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(3)

    // At 60s blurred, fetch runs
    await vi.advanceTimersByTimeAsync(BLURRED_POLL_INTERVAL_MS - FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(4)

    // Another 60s blurred, fetch runs again
    await vi.advanceTimersByTimeAsync(BLURRED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(5)

    // Switch back to focused state
    hasFocusSpy.mockReturnValue(true)
    window.dispatchEvent(new Event('focus'))

    // At 15s focused, fetch runs
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(6)

    release()
  })

  it('3 failures stop polling until visibility changes', async () => {
    mockedFetchConductors.mockRejectedValue(new Error('Network failure'))

    const release = acquireConductorsPoller()
    // 1st failure (immediate on acquire)
    await vi.waitFor(() => {
      expect(mockedFetchConductors).toHaveBeenCalledTimes(1)
      expect($conductors.get().failures).toBe(1)
      expect($conductors.get().status).toBe('error')
    })

    // 2nd failure
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    await vi.waitFor(() => {
      expect(mockedFetchConductors).toHaveBeenCalledTimes(2)
      expect($conductors.get().failures).toBe(2)
    })

    // 3rd failure
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    await vi.waitFor(() => {
      expect(mockedFetchConductors).toHaveBeenCalledTimes(3)
      expect($conductors.get().failures).toBe(MAX_CONSECUTIVE_FAILURES)
    })

    // Further timer ticks should NOT fire any more fetches
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS * 5)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(3)

    // Backend recovers
    mockedFetchConductors.mockResolvedValue(sampleResponse)

    // Visibility change (focus) resumes polling
    window.dispatchEvent(new Event('focus'))
    await vi.waitFor(() => {
      expect(mockedFetchConductors).toHaveBeenCalledTimes(4)
      expect($conductors.get().failures).toBe(0)
      expect($conductors.get().status).toBe('ready')
    })

    // Regular polling resumes
    await vi.advanceTimersByTimeAsync(FOCUSED_POLL_INTERVAL_MS)
    expect(mockedFetchConductors).toHaveBeenCalledTimes(5)

    release()
  })

  it('single-flight (never two fetches in flight)', async () => {
    let resolvePending!: (data: ConductorsResponse) => void
    mockedFetchConductors.mockImplementation(
      () =>
        new Promise(resolve => {
          resolvePending = resolve
        })
    )

    const p1 = refreshConductors()
    const p2 = refreshConductors()

    expect(mockedFetchConductors).toHaveBeenCalledTimes(1)
    expect($conductors.get().status).toBe('loading')

    resolvePending(sampleResponse)
    const [r1, r2] = await Promise.all([p1, p2])

    expect(r1).toEqual(sampleResponse)
    expect(r2).toEqual(sampleResponse)
    expect($conductors.get().status).toBe('ready')
    expect($conductors.get().data).toEqual(sampleResponse)
  })

  it('manual fresh refresh passes fresh=1', async () => {
    await refreshConductors({ fresh: true })
    expect(mockedFetchConductors).toHaveBeenCalledWith({ fresh: true })

    await refreshConductors()
    expect(mockedFetchConductors).toHaveBeenLastCalledWith({})
  })
})
