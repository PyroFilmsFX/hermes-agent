import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { $sessionStates } from '@/store/session-states'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'

import { RelayJobsSection } from './relay-jobs-section'

vi.stubGlobal(
  'ResizeObserver',
  class {
    disconnect() {}
    observe() {}
    unobserve() {}
  }
)

const runningJob = (overrides: Partial<RelayJob> = {}): RelayJob => ({
  durationSeconds: 0,
  jobId: 'w_running',
  lane: 'impl',
  model: 'gpt-6-sol',
  role: 'worker',
  spawnedAt: Date.now() - 17_000,
  status: 'running',
  worker: 'codex',
  ...overrides
})

afterEach(() => {
  cleanup()
  $relayJobsBySession.set({})
  $sessionStates.set({})
  vi.restoreAllMocks()
})

it('renders the relay badge, worker metadata, lane, and live elapsed time', () => {
  const now = vi.spyOn(Date, 'now').mockReturnValue(100_000)
  $relayJobsBySession.set({ owner: [runningJob({ spawnedAt: 83_000 })] })
  now.mockReturnValue(101_000)

  const { container } = render(<RelayJobsSection sessionId="owner" />)

  fireEvent.click(screen.getByRole('button', { name: /1 Relay job/ }))
  expect(screen.getByText('codex')).toBeTruthy()
  expect(screen.getByText('gpt-6-sol')).toBeTruthy()
  expect(screen.getByText(/impl · worker/)).toBeTruthy()
  expect(container.querySelector('[data-slot="relay-badge"]')?.textContent?.toLowerCase()).toBe('relay')
  expect(container.querySelector('[data-slot="composer-relay-jobs"]')?.textContent).toContain('18s')
})

it('shows finished relay jobs only while the parent turn remains live and uses stored duration', () => {
  $relayJobsBySession.set({
    owner: [runningJob({ durationSeconds: 42, spawnedAt: 1_000, status: 'succeeded' })]
  })
  const { rerender, container } = render(<RelayJobsSection sessionId="owner" />)

  expect(container.querySelector('[data-slot="composer-relay-jobs"]')).toBeNull()
  $sessionStates.set({
    owner: { busy: false, awaitingResponse: false, turnLive: true } as never
  })
  rerender(<RelayJobsSection sessionId="owner" />)

  expect(screen.getByRole('button', { name: /1 Relay job/ })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /1 Relay job/ }))
  expect(container.querySelector('[data-slot="composer-relay-jobs"]')?.textContent).toContain('42s')
  act(() => $sessionStates.set({ owner: { busy: false, awaitingResponse: false, turnLive: false } as never }))
  expect(container.querySelector('[data-slot="composer-relay-jobs"]')).toBeNull()
})
