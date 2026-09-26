import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({ visible: true }))
const requestForOwnedSession = vi.hoisted(() => vi.fn())

vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: () => state.visible }))
vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<typeof import('@/store/session-states')>()),
  knownOwnerForSession: () => null,
  requestForOwnedSession: (...args: unknown[]) => requestForOwnedSession(...args)
}))

import { ArtifactViewerPane } from '../artifact-viewer-pane'

const target = { sessionId: 'session-1', jobId: 'w_one' }
const list = (status: string) => ({
  durable: { state: 'unavailable', reason: 'not_configured' },
  local: [{ job_id: 'w_one', variant: 'log', bytes: 200, lines: 2, mtime: '', data_class: 'B', viewable: 'text', sha256: null, status, worker: 'codex' }],
  local_reason: 'none'
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  state.visible = true
  requestForOwnedSession.mockReset()
})

describe('artifact source live tail', () => {
  it('polls no faster than every two seconds and stops after a terminal result', async () => {
    vi.useFakeTimers()
    requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string) => method === 'conductor_artifacts.list'
      ? list('done')
      : { mode: 'text', text: 'tail\n', offset: 0, length: 5, total_bytes: 5, eof: true, bof: true, redacted: false })
    render(<ArtifactViewerPane target={target} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(requestForOwnedSession).toHaveBeenCalledTimes(2)
  })

  it('makes only three list and read calls in five seconds while visible and running', async () => {
    vi.useFakeTimers()
    requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string) => method === 'conductor_artifacts.list'
      ? list('running')
      : { mode: 'text', text: 'tail\n', offset: 0, length: 5, total_bytes: 5, eof: true, bof: true, redacted: false })
    render(<ArtifactViewerPane target={target} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(requestForOwnedSession.mock.calls.filter(call => call[2] === 'conductor_artifacts.list')).toHaveLength(3)
    expect(requestForOwnedSession.mock.calls.filter(call => call[2] === 'conductor_artifacts.read')).toHaveLength(3)
  })

  it('keeps loaded earlier pages when the live tail refreshes', async () => {
    vi.useFakeTimers()
    requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string, params: { from_end?: boolean; offset?: number }) => {
      if (method === 'conductor_artifacts.list') return list('running')
      if (params.from_end) return { mode: 'text', text: 'new tail\n', offset: 100, length: 100, total_bytes: 200, eof: true, bof: false, redacted: false }
      return { mode: 'text', text: 'earlier page\n', offset: 0, length: 100, total_bytes: 200, eof: false, bof: true, redacted: false }
    })
    render(<ArtifactViewerPane target={target} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier output' }))
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(screen.getByText(/earlier page/)).toBeTruthy()
    expect(screen.getByText(/new tail/)).toBeTruthy()
  })

  it('stops after three failures across a pane visibility toggle', async () => {
    vi.useFakeTimers()
    requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string) => {
      if (requestForOwnedSession.mock.calls.length > 2) throw new Error('read failed')
      if (method === 'conductor_artifacts.list') return list('running')
      return { mode: 'text', text: 'tail\n', offset: 0, length: 5, total_bytes: 5, eof: true, bof: true, redacted: false }
    })
    const view = render(<ArtifactViewerPane target={target} />)
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    state.visible = false
    view.rerender(<ArtifactViewerPane target={{ ...target }} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
    const afterThree = requestForOwnedSession.mock.calls.length
    state.visible = true
    view.rerender(<ArtifactViewerPane target={{ ...target }} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
    expect(afterThree).toBe(3)
    expect(requestForOwnedSession).toHaveBeenCalledTimes(5)
  })

  it('does not poll while the pane is hidden', async () => {
    vi.useFakeTimers()
    state.visible = false
    requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string) => method === 'conductor_artifacts.list'
      ? list('running')
      : { mode: 'text', text: 'tail\n', offset: 0, length: 5, total_bytes: 5, eof: true, bof: true, redacted: false })
    const view = render(<ArtifactViewerPane target={target} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(requestForOwnedSession).toHaveBeenCalledTimes(2)
    state.visible = true
    view.rerender(<ArtifactViewerPane target={{ ...target }} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(requestForOwnedSession).toHaveBeenCalledTimes(4)
  })
})
