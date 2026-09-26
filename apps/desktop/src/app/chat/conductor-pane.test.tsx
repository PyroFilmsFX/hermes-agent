import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $relayJobsBySession } from '@/store/composer-status'
import { $conductorBuildBySession, type ConductorBuild } from '@/store/conductor-build'

import { ConductorPane } from './conductor-pane'

const build: ConductorBuild = {
  armed_at: '2026-09-26T10:00:00Z', lanes_build_matched: 1, lanes_running: 1, lanes_stale: 0,
  lease_expires_at: 1_790_000_000, plan: 'PLAN.md', run_id: 'run-1', session_id: 'session-1',
    stage: null, state: 'waiting', unit_id: null, usd: null, usd_source: 'missing', wait_since: 5,
  waiting_on: 'worker response', wave_current: 3, waves_done: 2, waves_total: 7
}

afterEach(() => {
  cleanup()
  $conductorBuildBySession.set({})
  $relayJobsBySession.set({})
})

describe('conductor runs pane', () => {
  it('maps the active build and existing relay rows without rendering a cost value', () => {
    $conductorBuildBySession.set({ 'session-1': build })
    $relayJobsBySession.set({ 'session-1': [{
      durationSeconds: 0, jobId: 'w_one', lane: 'impl', model: 'gpt-6-sol', role: 'worker',
      spawnedAt: 10, status: 'running', worker: 'codex'
    }] })
    render(<ConductorPane sessionId="session-1" />)
    expect(screen.getByText('PLAN.md')).toBeTruthy()
    expect(screen.getByText('run-1')).toBeTruthy()
    expect(screen.getByText('Wave 3/7')).toBeTruthy()
    expect(screen.getByText('Done 2')).toBeTruthy()
    expect(screen.getByText('waiting')).toBeTruthy()
    expect(screen.getByText('worker response')).toBeTruthy()
    expect(screen.getByText('codex')).toBeTruthy()
    expect(screen.getByText('build')).toBeTruthy()
    expect(document.body.textContent).not.toContain('$')
  })

  it('renders the pane placeholders', () => {
    $conductorBuildBySession.set({ 'session-1': build })
    render(<ConductorPane sessionId="session-1" />)
    expect(screen.getByText('Cost: not metered')).toBeTruthy()
    expect(screen.getByText('Stage/unit: needs conductor T1')).toBeTruthy()
  })

  it('omits the wave row when wave values are unavailable', () => {
    $conductorBuildBySession.set({ 'session-1': { ...build, wave_current: null, waves_total: null } })
    render(<ConductorPane sessionId="session-1" />)
    expect(screen.queryByText('Wave')).toBeNull()
    expect(screen.queryByText(/Wave —/)).toBeNull()
  })

  it('shows the empty state when no build is armed', () => {
    $conductorBuildBySession.set({ 'session-1': null })
    render(<ConductorPane sessionId="session-1" />)
    expect(screen.getByText('No build armed in this workspace.')).toBeTruthy()
  })
})
