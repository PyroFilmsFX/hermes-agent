import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ConductorRowDetail } from './conductor-row-detail'
import { makeConductorRow } from './conductors-fixtures'
import { clockOf, dueDayOf } from './conductors-model'

const SINCE = '2026-09-29T07:12:00Z'
const DUE = '2026-09-30T12:00:00Z'

const full = () =>
  makeConductorRow('a', {
    orchestrator: { attribution: 'bound', profile: 'writer', live: 'cli' },
    build: {
      current_units: [
        { id: 'H5', title: 'attest.py verifier', seat: 'agy', since: SINCE },
        { id: 'H6', title: 'ledger', seat: 'codex', since: '' }
      ],
      remaining_waves: [{ index: 3, id: 'w3', title: 'Desktop', units: 4 }],
      gates: [
        { id: 'g1', kind: 'ci', label: 'CI run', since: SINCE, due: DUE, ref: null, state: 'waiting', waiter: true },
        { id: 'g2', kind: 'ci', label: 'Fork CI', since: '', due: null, ref: null, state: 'failed', waiter: false }
      ],
      owner_blockers: [
        { id: 'b1', action: 'approve', label: 'Approve the merge', since: SINCE, ref: null },
        { id: 'b2', action: 'relaunch', label: '', since: '', ref: null }
      ],
      refusals: [
        { seat: 'agy', code: 'stale_daemon', count: 2, last_at: '', note: 'old daemon' },
        { seat: 'agy', code: 'quota', count: 1, last_at: '', note: '' },
        { seat: 'codex', code: 'contract_gaps', count: 1, last_at: '', note: '' }
      ],
      lanes: { running: 3, stale: 1, cap: 6 }
    },
    other_builds: [
      { run_id: 'run-x', plan_title: 'Older plan', liveness: 'stale', waves: { done: 2, total: 5, current: 3 } },
      { run_id: 'run-y', plan_title: null, liveness: 'quiet', waves: { done: 0, total: 0, current: 0 } }
    ]
  })

const section = (container: HTMLElement, id: string) => container.querySelector(`[data-detail="${id}"]`)!

describe('expanded row detail (§8)', () => {
  it('lists every gate with kind, label, since, due and state; a failed CI gate is red', () => {
    const { container } = render(<ConductorRowDetail row={full()} />)
    const items = [...section(container, 'gates').querySelectorAll('li')].map(li => li.textContent)
    expect(items).toEqual([
      `CI · CI run · since ${clockOf(Date.parse(SINCE))} · due ${dueDayOf(Date.parse(DUE))} · waiting`,
      'CI · Fork CI · failed'
    ])
    expect(section(container, 'gates').querySelectorAll('li')[1].querySelector('.text-\\(--ui-red\\)')).toBeTruthy()
  })

  it('groups refusals by seat with code × count and the note', () => {
    const { container } = render(<ConductorRowDetail row={full()} />)
    const items = [...section(container, 'refusals').querySelectorAll('li')].map(li => li.textContent)
    expect(items).toEqual(['agy stale_daemon × 2 — old daemon; quota × 1', 'codex contract_gaps × 1'])
  })

  it('shows other builds, remaining waves, current units, full blockers, lanes and attribution', () => {
    const { container } = render(<ConductorRowDetail row={full()} />)
    const text = (id: string) => [...section(container, id).querySelectorAll('li')].map(li => li.textContent)

    expect(text('other-builds')).toEqual(['Older plan · W3/5 · Stale', 'run-y · Quiet'])
    expect(text('waves')).toEqual(['W3 · Desktop · 4 units'])
    expect(text('units')).toEqual([
      `H5 attest.py verifier · agy · since ${clockOf(Date.parse(SINCE))}`,
      'H6 ledger · codex'
    ])
    expect(text('blockers')).toEqual([`Approve the merge · since ${clockOf(Date.parse(SINCE))}`, 'relaunch'])
    expect(section(container, 'lanes').textContent).toBe('Lanes3 of 6 running · 1 stale')
    expect(section(container, 'attribution').textContent).toContain('Bound to the project · writer · in a terminal')
    expect(section(container, 'attribution').textContent).toContain('run-a · 6d739ad6')
  })

  it('says "None" for empty sections and falls back to the phase for units', () => {
    const { container } = render(
      <ConductorRowDetail row={makeConductorRow('b', { build: { current_units: [], gates: [], phase: 'review' } })} />
    )

    expect(section(container, 'units').textContent).toBe('Current unitsreview')
    expect(section(container, 'gates').textContent).toBe('GatesNone')
    expect(section(container, 'refusals').textContent).toBe('RefusalsNone')
    expect(section(container, 'other-builds').textContent).toBe('Other buildsNone')
    expect(container.querySelector('[data-detail="record-hint"]')).toBeNull()
  })

  it('explains a mismatched or newer-schema record in one line', () => {
    const { container, rerender } = render(
      <ConductorRowDetail row={makeConductorRow('m', { record: { reason: 'mismatched', valid: false } })} />
    )

    expect(container.querySelector('[data-detail="record-hint"]')?.textContent).toContain('different run')

    rerender(<ConductorRowDetail row={makeConductorRow('n', { record: { reason: 'newer_schema' } })} />)
    expect(container.querySelector('[data-detail="record-hint"]')?.textContent).toContain('newer schema')
  })

  it('lays its sections out in three, two or one column by container width', () => {
    const { container } = render(<ConductorRowDetail row={full()} />)
    const grid = container.querySelector('[data-slot="conductor-row-detail"] > .grid')!
    expect(grid.className).toContain('grid-cols-3')
    expect(grid.className).toContain('@max-[1100px]:grid-cols-2')
    expect(grid.className).toContain('@max-[700px]:grid-cols-1')
  })

  it('creates no timers', () => {
    const intervals = vi.spyOn(globalThis, 'setInterval')
    const timeouts = vi.spyOn(globalThis, 'setTimeout')
    render(<ConductorRowDetail row={full()} />)
    expect(intervals).not.toHaveBeenCalled()
    expect(timeouts).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })
})
