import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import * as gateway from '@/store/gateway'
import { _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $conductorBuildBySession } from '@/store/conductor-build'

import { useConductorBuild } from './use-conductor-build'

const SID = 'conductor-session'

function Harness() {
  useConductorBuild(SID)
  return <div />
}

function tree(visible: boolean) {
  return <PaneVisibleContext.Provider value={visible}><Harness /></PaneVisibleContext.Provider>
}

describe('useConductorBuild polling', () => {
  const request = vi.fn(async () => ({ build: null, unreadable: false }))

  beforeEach(() => {
    vi.useFakeTimers()
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    request.mockReset()
    request.mockResolvedValue({ build: null, unreadable: false })
    vi.spyOn(gateway, 'requestGatewayForAgent').mockImplementation(request as never)
    setSessionOwnerHint(SID, { connectionId: 'local', profile: 'default' })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
    $conductorBuildBySession.set({})
    _resetSessionOwnerHintsForTests()
  })

  it('polls every two seconds while armed and every ten seconds when clear', async () => {
    request.mockResolvedValue({ build: { run_id: 'run', plan: 'P' } as never, unreadable: false })
    render(tree(true))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(request).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(request).toHaveBeenCalledTimes(3)

    request.mockResolvedValue({ build: null, unreadable: false })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    const afterClear = request.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(9_999) })
    expect(request).toHaveBeenCalledTimes(afterClear)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(request).toHaveBeenCalledTimes(afterClear + 1)
  })

  it('pauses interval requests while the document is hidden', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    request.mockRejectedValue(new Error('offline'))
    render(tree(true))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(request).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('does not poll a hidden tab and resumes when it becomes visible', async () => {
    const view = render(tree(false))
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(request).not.toHaveBeenCalled()
    view.rerender(tree(true))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('stops issuing requests after three consecutive failures', async () => {
    request.mockRejectedValue(new Error('offline'))
    render(tree(true))
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(request).toHaveBeenCalledTimes(3)
  })
})
