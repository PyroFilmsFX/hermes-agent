import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConductorRow, LivenessChip } from './conductor-row'
import { ALL_DERIVED, GENERATED_AT, makeConductorRow } from './conductors-fixtures'
import { EMPTY_ROW_CACHE, isAbandonedRow, isDerivedColumn, reconcileRows, sortConductorRows } from './conductors-model'

const renderRow = (row = makeConductorRow('a'), abandoned = false) =>
  render(
    <div role="grid">
      <ConductorRow abandoned={abandoned} activeProfile="default" row={row} />
    </div>
  )

afterEach(() => {
  vi.restoreAllMocks()
})

describe('liveness chip (§4)', () => {
  it.each([
    ['active', 'Active'],
    ['quiet', 'Quiet'],
    ['idle', 'Idle'],
    ['stale', 'Stale'],
    ['abandoned', 'Abandoned']
  ] as const)('labels %s as "%s"', (state, label) => {
    const { container } = render(<LivenessChip state={state} />)
    const chip = container.querySelector(`[data-liveness="${state}"]`)
    expect(chip?.textContent).toBe(label)
  })

  it('shows the row liveness, and "Abandoned" for an abandoned row', () => {
    const { container, unmount } = renderRow(makeConductorRow('a', { build: { liveness: 'quiet' } }))
    expect(container.querySelector('[data-liveness]')?.textContent).toBe('Quiet')
    unmount()

    const gone = renderRow(makeConductorRow('b', { build: { liveness: 'stale' } }), true)
    expect(gone.container.querySelector('[data-liveness]')?.textContent).toBe('Abandoned')
  })
})

describe('≈ provenance mark (§3.6)', () => {
  const marksIn = (container: HTMLElement, col: string) =>
    container.querySelectorAll(`[data-col="${col}"] [data-derived]`).length

  it('renders no mark when every field came from the conductor record', () => {
    const { container } = renderRow()
    expect(container.querySelectorAll('[data-derived]')).toHaveLength(0)
  })

  it('marks only the columns §3.6 marks, never wave, estimate, CI/PR or last activity', () => {
    const row = makeConductorRow('a', {
      field_sources: ALL_DERIVED,
      build: {
        owner_blockers: [{ id: 'ob', action: 'approve', label: 'Approve', since: '', ref: null }],
        ci: [{ kind: 'run', ref: '1', branch: 'b', pr: 12, state: 'success', url: '', checked_at: '' }]
      }
    })

    const { container } = renderRow(row)

    for (const col of ['now', 'remaining', 'estimate', 'seats', 'lanes', 'blockers']) {
      expect(marksIn(container, col), col).toBe(1)
    }

    for (const col of ['project', 'session', 'ci', 'activity']) {
      expect(marksIn(container, col), col).toBe(0)
    }

    // The estimate column's only mark belongs to the gates chip, not the estimate value.
    expect(container.querySelector('[data-estimate] [data-derived]')).toBeNull()
    expect(screen.getAllByLabelText('Derived by Hermes; conductor did not report this').length).toBeGreaterThan(0)
  })

  it('claims nothing for a field the backend did not describe or reported as none', () => {
    const row = makeConductorRow('a', { field_sources: { gates: 'none' } })
    expect(isDerivedColumn(row, 'gates')).toBe(false)
    expect(isDerivedColumn(row, 'seats')).toBe(false)
    expect(isDerivedColumn({ ...row, field_sources: { seats: 'relay' } }, 'seats')).toBe(true)
  })

  it('shows "?" for native seats when seats are relay-derived', () => {
    const { container } = renderRow(makeConductorRow('a', { field_sources: { seats: 'relay' } }))
    expect(container.querySelector('[data-seat="sonnet"]')?.textContent).toContain('?')
    expect(container.querySelector('[data-seat="opus"]')?.textContent).toContain('?')
    expect(container.querySelector('[data-seat="agy"]')?.textContent).toContain('2')
  })
})

describe('row rendering', () => {
  it('is a grid row with one labelled gridcell per column', () => {
    renderRow()
    const row = screen.getByRole('row')
    expect(row.querySelectorAll('[role="gridcell"]')).toHaveLength(10)
  })

  it('shows the wave, units, estimate and lanes in the wide layout', () => {
    const { container } = renderRow()
    expect(container.querySelector('[data-col="now"]')?.textContent).toContain('W2/4')
    expect(container.querySelector('[data-col="now"]')?.textContent).toContain('H5 attest.py verifier')
    expect(container.querySelector('[data-col="remaining"]')?.textContent).toBe('3 waves · 12 units')
    expect(container.querySelector('[data-estimate]')?.textContent).toBe('6.5 h / 10 h')
    expect(container.querySelector('[data-col="estimate"]')?.textContent).toContain('1 gate')
    expect(container.querySelector('[data-col="lanes"]')?.textContent).toBe('3/6')
  })

  it('creates no timers', () => {
    const intervals = vi.spyOn(globalThis, 'setInterval')
    const timeouts = vi.spyOn(globalThis, 'setTimeout')

    const row = makeConductorRow('a', {
      field_sources: ALL_DERIVED,
      build: { refusals: [{ seat: 'agy', code: 'stale_daemon', count: 2, last_at: '', note: 'old' }] }
    })

    render(
      <div role="grid">
        <ConductorRow abandoned={false} activeProfile="default" row={row} />
        <ConductorRow abandoned activeProfile="default" row={makeConductorRow('b')} />
      </div>
    )

    expect(intervals).not.toHaveBeenCalled()
    expect(timeouts).not.toHaveBeenCalled()
  })
})

describe('model rules', () => {
  it('abandons only a gone owner whose lease expired over 7 days ago', () => {
    const eightDays = GENERATED_AT - 8 * 86_400
    const gone = makeConductorRow('a', {
      orchestrator: { live: 'none' },
      build: { liveness: 'stale', idle_since: eightDays }
    })
    expect(isAbandonedRow(gone, GENERATED_AT)).toBe(true)
    expect(isAbandonedRow({ ...gone, orchestrator: { ...gone.orchestrator, live: 'cli' } }, GENERATED_AT)).toBe(false)
    expect(isAbandonedRow({ ...gone, build: { ...gone.build, idle_since: GENERATED_AT - 86_400 } }, GENERATED_AT)).toBe(
      false
    )
  })

  it('sorts owner-blocked, failing CI, stale, idle, active, quiet', () => {
    const rows = [
      makeConductorRow('quiet', { build: { liveness: 'quiet' } }),
      makeConductorRow('active', { build: { liveness: 'active' } }),
      makeConductorRow('idle', { build: { liveness: 'idle' } }),
      makeConductorRow('stale', { build: { liveness: 'stale' } }),
      makeConductorRow('ci', {
        build: { ci: [{ kind: 'run', ref: '', branch: '', pr: null, state: 'failure', url: '', checked_at: '' }] }
      }),
      makeConductorRow('blocked', { build: { liveness: 'quiet', blocked: true } })
    ]

    expect(sortConductorRows(rows).map(row => row.key)).toEqual(['blocked', 'ci', 'stale', 'idle', 'active', 'quiet'])
  })

  it('keeps the previous object for an unchanged row', () => {
    const first = reconcileRows(EMPTY_ROW_CACHE, [makeConductorRow('a'), makeConductorRow('b')])
    const clone = JSON.parse(JSON.stringify(first.rows)) as typeof first.rows
    clone[1] = { ...clone[1], build: { ...clone[1].build, liveness: 'idle' } }
    const second = reconcileRows(first.cache, clone)
    expect(second.rows[0]).toBe(first.rows[0])
    expect(second.rows[1]).not.toBe(first.rows[1])
  })
})

describe('server abandoned flag', () => {
  it('wins over the local 7-day rule in both directions', async () => {
    const { isAbandonedRow } = await import('./conductors-model')
    const { makeConductorRow } = await import('./conductors-fixtures')
    const fresh = makeConductorRow('r1')
    expect(isAbandonedRow({ ...fresh, abandoned: true }, Date.now() / 1000)).toBe(true)
    expect(isAbandonedRow({ ...fresh, abandoned: false }, Date.now() / 1000 + 30 * 86400)).toBe(false)
  })
})
