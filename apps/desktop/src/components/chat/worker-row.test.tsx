import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RelayJob } from '@/store/composer-status'

import { WorkerRow } from './worker-row'

afterEach(cleanup)

const job = (overrides: Partial<RelayJob> = {}): RelayJob => ({
  buildMatch: false,
  durationSeconds: 1264,
  effort: 'high',
  jobId: 'w_20260926T202751Z_0e14',
  label: 'fix-g9',
  lane: 'fix',
  model: 'gpt-6-luna',
  place: 'lane-g9',
  purpose: 'Fix exactly these review findings in HE-G9 commit c0deb07a65',
  role: 'overflow',
  spawnedAt: 1_000,
  status: 'succeeded',
  worker: 'codex',
  ...overrides
})

describe('WorkerRow', () => {
  it('shows name, purpose, seat and model, kind and place, but no id or routing role', () => {
    const { container } = render(<WorkerRow job={job()} nowMs={2_000} onActivate={vi.fn()} />)

    expect(screen.getByText('fix-g9')).toBeTruthy()
    expect(screen.getByText('Fix exactly these review findings in HE-G9 commit c0deb07a65')).toBeTruthy()
    expect(container.textContent).toContain('Codex · GPT-6 Luna')
    expect(container.textContent).toContain('Fix')
    expect(container.textContent).toContain('lane-g9')
    expect(container.textContent).toContain('cx')
    expect(container.textContent).toContain('Done')
    expect(container.textContent).not.toContain('w_20260926T202751Z_0e14')
    expect(container.textContent).not.toContain('overflow')
  })

  it('falls back from label to place to kind for the name', () => {
    const { rerender } = render(<WorkerRow job={job({ label: '' })} nowMs={2_000} onActivate={vi.fn()} />)

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Open output for lane-g9, Codex, Done')
    rerender(<WorkerRow job={job({ label: '', place: '', lane: 'validate' })} nowMs={2_000} onActivate={vi.fn()} />)
    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Open output for Review, Codex, Done')
  })

  it('explains failures and timeouts without a raw signal number', () => {
    const { container, rerender } = render(
      <WorkerRow job={job({ exitCode: undefined, status: 'failed' })} nowMs={2_000} onActivate={vi.fn()} />
    )

    expect(container.textContent).toContain('no exit code')
    rerender(<WorkerRow job={job({ exitCode: 1, status: 'failed' })} nowMs={2_000} onActivate={vi.fn()} />)
    expect(container.textContent).toContain('exit 1')
    rerender(<WorkerRow job={job({ exitCode: -15, status: 'timeout' })} nowMs={2_000} onActivate={vi.fn()} />)
    expect(container.textContent).toContain('stopped after the time limit')
    expect(container.textContent).not.toContain('-15')
  })

  it('omits elapsed for a finished row with no recorded duration', () => {
    const { container } = render(
      <WorkerRow job={job({ durationSeconds: undefined })} nowMs={2_000} onActivate={vi.fn()} />
    )

    expect(container.querySelector('[data-slot="worker-row-elapsed"]')).toBeNull()
  })

  it('activates with a click and with the keyboard', () => {
    const onActivate = vi.fn()
    render(<WorkerRow job={job()} nowMs={2_000} onActivate={onActivate} />)
    const row = screen.getByRole('button', { name: 'Open output for fix-g9, Codex, Done' })

    fireEvent.click(row)
    fireEvent.keyDown(row, { key: 'Enter' })
    fireEvent.keyDown(row, { key: ' ' })
    expect(onActivate).toHaveBeenCalledTimes(3)
  })
})
