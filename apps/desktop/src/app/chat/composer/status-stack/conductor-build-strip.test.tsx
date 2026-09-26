import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ConductorBuild } from '@/store/conductor-build'

import { formatClock, formatStamp, idleSentence, markerStaleSentence } from '@/lib/conductor-time'

import { ConductorBuildStrip } from './conductor-build-strip'

const RUN = '484496' + 'a'.repeat(20) + 'c677d2'

const base: ConductorBuild = {
  armed_at: '2026-09-26T08:58:00Z', lanes_build_matched: 1, lanes_running: 2, lanes_stale: 0,
  lease_expires_at: '2026-09-26T14:58:00Z', plan: 'OVERNIGHT-HERMES-WORKER-2026-09-26', run_id: RUN,
  session_id: '20260924_200208_c68a80', stage: null, state: 'active', unit_id: null, usd: null,
  usd_source: 'missing', wait_since: 0, waiting_on: '', wave_current: 3, waves_done: 2, waves_total: 7
}

afterEach(cleanup)

const strip = (container: HTMLElement) => container.querySelector('[data-slot="conductor-build-strip"]') as HTMLElement

describe('conductor build status strip', () => {
  it('stays hidden without an armed build', () => {
    const { container } = render(<ConductorBuildStrip build={null} onOpen={vi.fn()} />)
    expect(container.firstChild).toBeNull()
  })

  it('leads with the plan title, then the wave track, state chip and count, and opens the pane', () => {
    const onOpen = vi.fn()
    const { container } = render(<ConductorBuildStrip build={base} onOpen={onOpen} />)

    expect(screen.getByText('Overnight Hermes Worker')).toBeTruthy()
    expect(screen.getByRole('img', { name: 'Wave 3 of 7, 2 closed' })).toBeTruthy()
    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Running')
    expect(screen.getByText('2 running')).toBeTruthy()
    fireEvent.click(strip(container))
    expect(onOpen).toHaveBeenCalledOnce()
  })

  it('never prints the system name, the raw stem, the run hash or a cost', () => {
    const { container } = render(<ConductorBuildStrip build={base} onOpen={vi.fn()} />)
    const text = container.textContent ?? ''

    expect(text).not.toContain('tb-build')
    expect(text).not.toContain('OVERNIGHT-HERMES-WORKER')
    expect(text).not.toMatch(/[0-9a-f]{32}/)
    expect(text).not.toContain('$')
    expect(text).not.toMatch(/W\d+\/\d+/)
  })

  it.each(['blocked', 'lease_expired'] as const)('does not dim the %s state', state => {
    const { container } = render(<ConductorBuildStrip build={{ ...base, lanes_running: 0, state }} onOpen={vi.fn()} />)

    expect(strip(container).dataset.state).toBe(state)
    expect(strip(container).className).not.toMatch(/opacity|text-\(--ui-text-tertiary\)/)
    expect(screen.getByText('Overnight Hermes Worker').className).toContain('--ui-text-primary')
    expect(container.textContent).not.toContain('nothing running')
  })

  it('names the lease-expired state and the blocked state on the chip', () => {
    const { container, rerender } = render(
      <ConductorBuildStrip build={{ ...base, state: 'lease_expired' }} onOpen={vi.fn()} />
    )

    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Lease expired')
    rerender(<ConductorBuildStrip build={{ ...base, state: 'blocked' }} onOpen={vi.fn()} />)
    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Blocked')
  })

  it.each(['active', 'waiting'] as const)('says nothing running for an idle %s build', state => {
    render(<ConductorBuildStrip build={{ ...base, lanes_running: 0, state, waiting_on: 'reviewer' }} onOpen={vi.fn()} />)

    expect(screen.getByText('nothing running')).toBeTruthy()
  })

  it('adds the not-responding clause next to a warning glyph', () => {
    const { container } = render(<ConductorBuildStrip build={{ ...base, lanes_stale: 1 }} onOpen={vi.fn()} />)

    expect(container.textContent).toContain('2 running, 1 not responding')
    expect(container.querySelector('.codicon-warning')).toBeTruthy()
  })

  it('omits the wave track without a usable wave total', () => {
    const { container } = render(
      <ConductorBuildStrip build={{ ...base, wave_current: null, waves_total: null }} onOpen={vi.fn()} />
    )

    expect(container.querySelector('[data-slot="wave-track"]')).toBeNull()
    expect(container.textContent).not.toContain('W0/0')
  })

  it('renders an idle build as a quiet "Build idle since HH:MM" chip', () => {
    const since = new Date()
    since.setHours(9, 58, 0, 0)
    const { container } = render(
      <ConductorBuildStrip
        build={{ ...base, idle_since: since.getTime() / 1000, lanes_running: 0, state: 'idle' }}
        onOpen={vi.fn()}
      />
    )
    const chip = container.querySelector('[data-slot="status-chip"]') as HTMLElement

    expect(chip.textContent).toBe(`Build idle since ${formatClock(since)}`)
    expect(chip.dataset.tone).toBe('idle')
    expect(chip.querySelector('.codicon-circle-outline')).toBeTruthy()
    expect(chip.innerHTML).not.toMatch(/--ui-(yellow|red|purple|blue|green)/)
    expect(container.textContent).not.toContain('nothing running')
    expect(container.textContent).not.toContain('Marker not updated')
  })

  it('adds a quiet "Marker not updated since" hint for a live build, never a warning', () => {
    const at = new Date()
    at.setHours(0, 5, 0, 0)
    const { container } = render(
      <ConductorBuildStrip build={{ ...base, marker_stale_since: at.getTime() / 1000 }} onOpen={vi.fn()} />
    )
    const hint = container.querySelector('[data-slot="conductor-build-marker-hint"]') as HTMLElement

    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Running')
    expect(hint.textContent).toBe(` · Marker not updated since ${formatClock(at)}`)
    expect(hint.innerHTML).not.toMatch(/warning|--ui-yellow/)
  })

  it('says the day when the idle or marker time is not today', () => {
    const now = new Date(2026, 8, 26, 12, 0).getTime()
    const yesterday = new Date(2026, 8, 25, 21, 4)

    expect(idleSentence(yesterday.getTime() / 1000, now)).toBe(`Build idle since ${formatStamp(yesterday)}`)
    expect(markerStaleSentence(yesterday.getTime() / 1000, now)).toBe(`Marker not updated since ${formatStamp(yesterday)}`)
    expect(idleSentence(null, now)).toBe('Build idle')
    expect(markerStaleSentence(null, now)).toBeNull()
  })
})
