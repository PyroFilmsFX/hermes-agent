import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const requestForOwnedSession = vi.fn()

vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: () => true }))
vi.mock('@/store/session-states', () => ({
  knownOwnerForSession: () => null,
  requestForOwnedSession: (...args: unknown[]) => requestForOwnedSession(...args)
}))

import { ArtifactViewerPane } from './artifact-viewer-pane'

afterEach(() => {
  cleanup()
  requestForOwnedSession.mockReset()
})

describe('artifact viewer pane', () => {
  it('shows the local log banner, source list, and text window', async () => {
    requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string) => {
      if (method === 'conductor_artifacts.list') {
        return {
          durable: { state: 'unavailable', reason: 'not_configured' },
          local: [{ job_id: 'w_one', variant: 'log', bytes: 12, lines: 2, mtime: '', data_class: 'B', viewable: 'text', sha256: null, status: 'running', worker: 'codex' }],
          local_reason: 'none'
        }
      }
      return { mode: 'text', text: 'latest line\n', offset: 0, length: 12, total_bytes: 12, eof: true, bof: true, redacted: false }
    })
    render(<ArtifactViewerPane target={{ sessionId: 'session-1', jobId: 'w_one' }} />)
    expect(await screen.findByText('Not uploaded to cntrl yet: showing local log')).toBeTruthy()
    expect(screen.getByText('local')).toBeTruthy()
    expect(await screen.findByText('latest line')).toBeTruthy()
    expect(screen.getByText(/codex · w_one · running/)).toBeTruthy()
  })

  it('shows only the metadata card for class A', async () => {
    requestForOwnedSession.mockImplementation(async () => ({
      durable: { state: 'unavailable', reason: 'not_configured' },
      local: [{ job_id: 'w_one', variant: 'log', bytes: 20, lines: 1, mtime: '', data_class: 'A', viewable: 'metadata', sha256: 'abc123', status: 'done', worker: 'codex' }],
      local_reason: 'none'
    }))
    render(<ArtifactViewerPane target={{ sessionId: 'session-1', jobId: 'w_one' }} />)
    expect(await screen.findByText('abc123')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /download/i })).toBeNull()
    expect(screen.queryByText(/latest line/)).toBeNull()
  })
})
