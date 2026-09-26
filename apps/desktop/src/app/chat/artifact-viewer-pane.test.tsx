import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const requestForOwnedSession = vi.fn()

vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: () => true }))
vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<typeof import('@/store/session-states')>()),
  knownOwnerForSession: () => null,
  requestForOwnedSession: (...args: unknown[]) => requestForOwnedSession(...args)
}))

import { $relayJobsBySession } from '@/store/composer-status'

import { ArtifactViewerPane } from './artifact-viewer-pane'

const JOB = 'w_20260926T202751Z_0e14'
const line = (value: unknown) => JSON.stringify(value)
const CODEX_LOG = [
  line({ type: 'thread.started', thread_id: 't' }),
  line({ type: 'item.completed', item: { id: 'a', type: 'agent_message', text: 'Looking at the findings.' } }),
  line({ type: 'item.completed', item: { id: 'b', type: 'command_execution', command: "/bin/zsh -lc 'git diff --stat'", aggregated_output: '', exit_code: 0, status: 'completed' } }),
  line({ type: 'item.completed', item: { id: 'c', type: 'agent_message', text: 'Fixed P0-1: the tail now polls once per 2 s.' } }),
  ''
].join('\n')

const source = (overrides: Record<string, unknown> = {}) => ({
  bytes: 12_288, data_class: 'B', job_id: JOB, lines: 4, mtime: '', sha256: null, status: 'succeeded',
  variant: 'log', viewable: 'text', worker: 'codex', ...overrides
})

function serve(sources: Array<Record<string, unknown>>, text = CODEX_LOG) {
  requestForOwnedSession.mockImplementation(async (_sid: string, _fallback: unknown, method: string) => {
    if (method === 'conductor_artifacts.list') {
      return { durable: { state: 'unavailable', reason: 'not_configured' }, local: sources, local_reason: 'none' }
    }

    return { bof: true, eof: true, length: text.length, mode: 'text', offset: 0, redacted: false, text, total_bytes: text.length }
  })
}

const hints = {
  durationSeconds: 1264, effort: 'high', label: 'fix-g9', lane: 'fix', model: 'gpt-6-luna', place: 'lane-g9',
  purpose: 'Fix exactly these review findings in HE-G9 commit c0deb07a65', status: 'succeeded', worker: 'codex'
}

afterEach(() => {
  cleanup()
  requestForOwnedSession.mockReset()
  $relayJobsBySession.set({})
})

describe('worker output pane', () => {
  it('heads with the worker row, never the job id, a local badge or cntrl sync copy', async () => {
    serve([source()])
    const { container } = render(<ArtifactViewerPane target={{ hints, jobId: JOB, sessionId: 'session-1' }} />)

    expect(screen.getByText('fix-g9')).toBeTruthy()
    expect(screen.getByText('Fix exactly these review findings in HE-G9 commit c0deb07a65')).toBeTruthy()
    await screen.findByText('Final report')
    const text = container.textContent ?? ''

    expect(text).not.toContain(JOB)
    expect(text).not.toMatch(/\blocal\b/)
    expect(text).not.toContain('Not uploaded')
    expect(text).not.toMatch(/class [ABC]/)
    expect(screen.getByRole('button', { name: 'Download log' })).toBeTruthy()
  })

  it('pins the final report and the result line for a finished codex worker', async () => {
    serve([source()])
    render(<ArtifactViewerPane target={{ hints, jobId: JOB, sessionId: 'session-1' }} />)

    expect(await screen.findByText('Final report')).toBeTruthy()
    expect(screen.getAllByText('Fixed P0-1: the tail now polls once per 2 s.').length).toBeGreaterThan(0)
    expect(screen.getByText('Done in 21:04')).toBeTruthy()
    expect(screen.getByText('$ git diff --stat')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Readable' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Raw' }))
    expect(screen.queryByText('Final report')).toBeNull()
    expect(document.querySelector('[data-slot="artifact-text-window"]')?.textContent).toContain('thread.started')
  })

  it('has no final report while the worker is still running', async () => {
    serve([source({ status: 'running' })])
    render(<ArtifactViewerPane target={{ hints: { ...hints, status: 'running' }, jobId: JOB, sessionId: 'session-1' }} />)

    expect(await screen.findByText('$ git diff --stat')).toBeTruthy()
    expect(screen.queryByText('Final report')).toBeNull()
  })

  it('renders plain logs as text without the readable toggle or a report block', async () => {
    serve([source({ worker: 'agy' }), source({ variant: 'agy_log', worker: 'agy' })], '# Report\n\nAll done.\n')
    render(<ArtifactViewerPane target={{ hints: { ...hints, worker: 'agy' }, jobId: JOB, sessionId: 'session-1' }} />)

    expect(await screen.findByText(/All done\./)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Readable' })).toBeNull()
    expect(screen.queryByText('Final report')).toBeNull()
    expect(screen.getByRole('button', { name: 'Transcript' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Debug log' })).toBeTruthy()
  })

  it('shows one sentence for a sensitive (metadata-only) log', async () => {
    serve([source({ bytes: 1_258_291, data_class: 'A', lines: 4210, sha256: 'abc123', viewable: 'metadata' })])
    const { container } = render(<ArtifactViewerPane target={{ hints, jobId: JOB, sessionId: 'session-1' }} />)

    expect(
      await screen.findByText('This log is marked sensitive, so only its size is shown here: 1.2 MB, 4,210 lines.')
    ).toBeTruthy()
    expect(container.textContent).not.toContain('abc123')
    expect(container.textContent).not.toContain('unknown')
    expect(container.textContent).not.toMatch(/class A/)
  })

  it('omits the line count when it is unknown', async () => {
    serve([source({ bytes: 1_258_291, data_class: 'A', lines: null, viewable: 'metadata' })])
    render(<ArtifactViewerPane target={{ hints, jobId: JOB, sessionId: 'session-1' }} />)

    expect(await screen.findByText('This log is marked sensitive, so only its size is shown here: 1.2 MB.')).toBeTruthy()
  })

  it('says when a worker has not written anything yet', async () => {
    serve([source({ status: 'running' })], '')
    render(<ArtifactViewerPane target={{ hints: { ...hints, status: 'running' }, jobId: JOB, sessionId: 'session-1' }} />)

    expect(await screen.findByText('This worker has not written any output yet.')).toBeTruthy()
  })
})
