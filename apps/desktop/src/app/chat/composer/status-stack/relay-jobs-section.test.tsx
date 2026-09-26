import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { $artifactViewerTarget } from '@/store/artifact-viewer'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'
import { $sessionStates } from '@/store/session-states'

import { RelayJobsSection } from './relay-jobs-section'

vi.stubGlobal(
  'ResizeObserver',
  class {
    disconnect() {}
    observe() {}
    unobserve() {}
  }
)

let seq = 0

const worker = (overrides: Partial<RelayJob> = {}): RelayJob => ({
  buildMatch: false,
  durationSeconds: 42,
  effort: 'high',
  jobId: `w_20260926T2027${String(seq++).padStart(2, '0')}Z_0e14`,
  label: 'fix-g9',
  lane: 'fix',
  model: 'gpt-6-luna',
  place: 'lane-g9',
  purpose: 'Fix exactly these review findings',
  role: 'overflow',
  spawnedAt: 1_000,
  status: 'succeeded',
  worker: 'codex',
  ...overrides
})

afterEach(() => {
  cleanup()
  $relayJobsBySession.set({})
  $sessionStates.set({})
  $artifactViewerTarget.set(null)
  vi.restoreAllMocks()
})

const names = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('[data-slot="worker-row"] .font-medium')).map(node => node.textContent)

it('titles the section Workers with a count summary and no relay eyebrow', () => {
  $relayJobsBySession.set({
    owner: [
      worker({ label: 'impl-u81d0', status: 'running' }),
      worker({ label: 'impl-g9', status: 'failed' }),
      worker({ label: 'hung', status: 'stale' }),
      worker({ label: 'done' })
    ]
  })
  const { container } = render(<RelayJobsSection sessionId="owner" />)

  expect(screen.getByRole('button', { name: /Workers/ })).toBeTruthy()
  expect(container.textContent).toContain('1 running, 1 failed, 1 not responding')
  expect(container.textContent?.toLowerCase()).not.toContain('relay')
  expect(container.textContent).not.toContain('overflow')
})

it('keeps finished rows after the turn ends', () => {
  $sessionStates.set({ owner: { busy: false, awaitingResponse: false, turnLive: false } as never })
  $relayJobsBySession.set({ owner: [worker({ label: 'finished' })] })
  const { container } = render(<RelayJobsSection sessionId="owner" />)

  expect(container.querySelector('[data-slot="composer-relay-jobs"]')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Workers/ }))
  expect(names(container)).toEqual(['finished'])
  expect(container.textContent).toContain('42s')
})

it('previews running and failing workers while collapsed, capped at three', () => {
  $relayJobsBySession.set({
    owner: [
      worker({ label: 'ok-one' }),
      worker({ label: 'run-a', spawnedAt: 5_000, status: 'running' }),
      worker({ label: 'fail-a', status: 'failed' }),
      worker({ label: 'run-b', spawnedAt: 4_000, status: 'running' }),
      worker({ label: 'late', status: 'timeout' }),
      worker({ label: 'hung', status: 'stale' })
    ]
  })
  const { container } = render(<RelayJobsSection sessionId="owner" />)

  expect(names(container)).toEqual(['run-a', 'run-b', 'fail-a'])
  expect(container.textContent).toContain('+2 more')
  expect(container.textContent).not.toContain('ok-one')
})

it('orders running, then needs attention, then done, newest first in each group', () => {
  $relayJobsBySession.set({
    owner: [
      worker({ label: 'done-old', spawnedAt: 1_000 }),
      worker({ label: 'done-new', spawnedAt: 9_000 }),
      worker({ label: 'fail', spawnedAt: 2_000, status: 'failed' }),
      worker({ label: 'run', spawnedAt: 3_000, status: 'running' })
    ]
  })
  const { container } = render(<RelayJobsSection sessionId="owner" />)

  fireEvent.click(screen.getByRole('button', { name: /Workers/ }))
  expect(names(container)).toEqual(['run', 'fail', 'done-new', 'done-old'])
})

it('opens the worker output from the whole row', () => {
  const job = worker({ label: 'fix-g9', status: 'running' })
  $relayJobsBySession.set({ owner: [job] })
  render(<RelayJobsSection sessionId="owner" />)

  fireEvent.click(screen.getByRole('button', { name: 'Open output for fix-g9, Codex, Running' }))
  expect($artifactViewerTarget.get()).toMatchObject({ jobId: job.jobId, sessionId: 'owner' })
  expect(screen.queryByText('Open transcript')).toBeNull()
})

it('is hidden with no workers', () => {
  const { container } = render(<RelayJobsSection sessionId="owner" />)

  expect(container.firstChild).toBeNull()
})
