import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { $conductorsPaneOpen } from '@/app/conductors/pane-state'
import type { ConductorBuild } from '@/store/conductor-build'

import { ConductorBuildStrip } from './conductor-build-strip'

const base: ConductorBuild = {
  armed_at: '',
  lanes_build_matched: 1,
  lanes_running: 3,
  lanes_stale: 0,
  lease_expires_at: 10,
  plan: 'PLAN',
  run_id: 'run',
  session_id: 'session',
  stage: null,
  state: 'waiting',
  unit_id: null,
  usd: null,
  usd_source: 'missing',
  wait_since: 5,
  waiting_on: 'reviewer',
  wave_current: 3,
  waves_done: 2,
  waves_total: 7
}

describe('conductor build status strip', () => {
  it('stays hidden without an armed build', () => {
    const { container } = render(<ConductorBuildStrip build={null} onOpen={vi.fn()} />)
    expect(container.firstChild).toBeNull()
  })

  it('shows the specified label and opens the runs pane when clicked', () => {
    const onOpen = vi.fn()
    render(<ConductorBuildStrip build={base} onOpen={onOpen} />)
    const button = screen.getByRole('button', { name: 'tb-build · PLAN · W3/7 · waiting · 3 lanes' })
    expect(button).toBeTruthy()
    fireEvent.click(button)
    expect(onOpen).toHaveBeenCalledOnce()
    expect(document.body.textContent).not.toContain('$')
  })

  it('omits the wave segment when wave values are unavailable', () => {
    render(<ConductorBuildStrip build={{ ...base, wave_current: null, waves_total: null }} onOpen={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'tb-build · PLAN · waiting · 3 lanes' })).toBeTruthy()
    expect(document.body.textContent).not.toContain('W0/0')
  })

  it.each(['blocked', 'lease_expired'] as const)('dims the %s state', state => {
    const { container } = render(<ConductorBuildStrip build={{ ...base, state }} onOpen={vi.fn()} />)
    expect(container.querySelector('[data-state="dimmed"]')).toBeTruthy()
  })

  it('renders state "stale" muted with the label "Stale build (not marked done)"', () => {
    const { container } = render(
      <ConductorBuildStrip build={{ ...base, state: 'stale' as ConductorBuild['state'] }} onOpen={vi.fn()} />
    )
    const button = screen.getByRole('button', {
      name: 'tb-build · PLAN · W3/7 · Stale build (not marked done) · 3 lanes'
    })
    expect(button).toBeTruthy()
    expect(container.querySelector('[data-state="dimmed"]')).toBeTruthy()
  })
})

describe('conductor build strip overflow', () => {
  it('"View all conductors" opens the Conductors page without opening the runs pane', () => {
    $conductorsPaneOpen.set(false)
    const onOpen = vi.fn()
    render(<ConductorBuildStrip build={base} onOpen={onOpen} />)
    fireEvent.click(screen.getByRole('button', { name: 'View all conductors' }))
    expect($conductorsPaneOpen.get()).toBe(true)
    expect(onOpen).not.toHaveBeenCalled()
    $conductorsPaneOpen.set(false)
  })
})
