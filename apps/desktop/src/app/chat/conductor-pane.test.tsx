import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { formatClock } from '@/lib/conductor-time'
import { $artifactViewerTarget } from '@/store/artifact-viewer'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'
import { $conductorBuildBySession, type ConductorBuild } from '@/store/conductor-build'
import * as gateway from '@/store/gateway'
import { _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'

import { ConductorPane } from './conductor-pane'

const RUN = '484496' + 'a'.repeat(19) + 'c677d27'

const build: ConductorBuild = {
  armed_at: '2026-09-26T08:58:00Z', lanes_build_matched: 1, lanes_running: 1, lanes_stale: 0,
  lease_expires_at: '2026-09-26T14:58:00Z', plan: 'OVERNIGHT-HERMES-WORKER-2026-09-26', run_id: RUN,
  session_id: '20260924_200208_c68a80', stage: null, state: 'lease_expired', unit_id: null, usd: null,
  usd_source: 'missing', wait_since: 0, waiting_on: '', wave_current: 1, waves_done: 0, waves_total: 7
}

const wireRow = (overrides: Record<string, unknown> = {}) => ({
  build_match: true, duration_sec: 242, effort: 'high', exit_code: null, heartbeat_at: '',
  job_id: 'w_20260926T202751Z_0e14', label: 'mkfix', lane: 'fix', model: 'gpt-6-luna', model_resolved: 'unverified',
  place: 'lane-mk', purpose: 'Fix the marker lease renewal', role: '', spawned_at: '2026-09-26T20:27:51Z',
  status: 'running', worker: 'codex', ...overrides
})

const recent = (overrides: Partial<RelayJob> = {}): RelayJob => ({
  buildMatch: false, durationSeconds: 60, effort: '', jobId: 'w_20260926T100000Z_aaaa', label: 'impl41',
  lane: 'impl', model: 'gpt-6-sol', place: 'lane-41', purpose: 'Implement bug 41', role: 'overflow',
  spawnedAt: 1_000, status: 'failed', worker: 'codex', ...overrides
})

const request = vi.fn()

beforeEach(() => {
  request.mockReset()
  request.mockResolvedValue({ jobs: [wireRow()] })
  vi.spyOn(gateway, 'requestGatewayForAgent').mockImplementation(request as never)
  setSessionOwnerHint('session-1', { connectionId: 'local', profile: 'default' })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  $conductorBuildBySession.set({})
  $relayJobsBySession.set({})
  $artifactViewerTarget.set(null)
  _resetSessionOwnerHintsForTests()
})

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

const textOutsideDetails = (container: HTMLElement) => {
  const clone = container.cloneNode(true) as HTMLElement
  clone.querySelector('[data-slot="conductor-build-details"]')?.remove()

  return clone.textContent ?? ''
}

describe('conductor pane', () => {
  it('heads with the plan title, state chip, wave track and the lease sentence', async () => {
    $conductorBuildBySession.set({ 'session-1': build })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()

    expect(screen.getByText('Overnight Hermes Worker')).toBeTruthy()
    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Lease expired')
    expect(screen.getByRole('img', { name: 'Wave 1 of 7, none closed' })).toBeTruthy()
    expect(screen.getByText('Wave 1 of 7')).toBeTruthy()
    expect(container.textContent).toMatch(/The conductor stopped renewing this build at .+\. Resume the conductor session to continue\./)
  })

  it('says the build has been idle since the lease ran out', async () => {
    const since = new Date()
    since.setHours(9, 58, 0, 0)
    $conductorBuildBySession.set({ 'session-1': { ...build, idle_since: since.getTime() / 1000, state: 'idle' } })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()

    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Idle')
    expect(container.textContent).toContain(`Build idle since ${formatClock(since)}`)
    expect(container.textContent).not.toContain('stopped renewing')
    expect(container.querySelector('[data-slot="conductor-marker-hint"]')).toBeNull()
  })

  it('shows a live build normally with a quiet marker hint under the header', async () => {
    const at = new Date()
    at.setHours(0, 5, 0, 0)
    $conductorBuildBySession.set({
      'session-1': { ...build, marker_stale_since: at.getTime() / 1000, state: 'active' }
    })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()

    expect(container.querySelector('[data-slot="status-chip"]')?.textContent).toBe('Running')
    const hint = container.querySelector('[data-slot="conductor-marker-hint"]') as HTMLElement
    expect(hint.textContent).toBe(`Marker not updated since ${formatClock(at)}`)
    expect(hint.className).toContain('--ui-text-tertiary')
  })

  it('carries no placeholders, raw ids or debug labels outside Build details', async () => {
    $conductorBuildBySession.set({ 'session-1': build })
    $relayJobsBySession.set({ 'session-1': [recent()] })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()
    const text = textOutsideDetails(container)

    for (const banned of ['not metered', 'needs conductor T1', 'Done 0', 'Run artifacts', 'tb-build', 'unverified', 'overflow']) {
      expect(text).not.toContain(banned)
    }
    expect(text).not.toMatch(/[0-9a-f]{32}/)
    expect(text).not.toMatch(/w_\d{8}T\d{6}Z_[0-9a-f]{4}/)
    expect(text).not.toContain('OVERNIGHT-HERMES-WORKER')
    expect(container.textContent).not.toContain('$')
  })

  it('splits This build (fetched with scope build) from Other recent work (buildMatch false)', async () => {
    $conductorBuildBySession.set({ 'session-1': build })
    $relayJobsBySession.set({ 'session-1': [recent(), recent({ buildMatch: true, jobId: 'w_20260926T100000Z_bbbb', label: 'dup' })] })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()

    const call = request.mock.calls.find(args => args.includes('relay_jobs.list'))

    expect(call).toContainEqual({ scope: 'build', session_id: 'session-1' })
    const thisBuild = container.querySelector('[data-slot="conductor-group-build"]') as HTMLElement
    const other = container.querySelector('[data-slot="conductor-group-other"]') as HTMLElement

    expect(thisBuild.textContent).toContain('This build')
    expect(thisBuild.textContent).toContain('1 worker, 1 running')
    expect(thisBuild.textContent).toContain('mkfix')
    expect(other.textContent).toContain('Other recent work')
    expect(other.textContent).toContain('1 worker, 1 failed')
    expect(other.textContent).not.toContain('impl41')
    expect(other.textContent).not.toContain('dup')
    fireEvent.click(screen.getByRole('button', { name: /Other recent work/ }))
    expect(other.textContent).toContain('impl41')
  })

  it('opens a worker output from its row', async () => {
    $conductorBuildBySession.set({ 'session-1': build })
    render(<ConductorPane sessionId="session-1" />)
    await flush()

    fireEvent.click(screen.getByRole('button', { name: 'Open output for mkfix, Codex, Running' }))
    expect($artifactViewerTarget.get()).toMatchObject({ jobId: 'w_20260926T202751Z_0e14', sessionId: 'session-1' })
  })

  it('says so when the build has no workers and expands other work', async () => {
    request.mockResolvedValue({ jobs: [] })
    $conductorBuildBySession.set({ 'session-1': build })
    $relayJobsBySession.set({ 'session-1': [recent()] })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()

    expect(screen.getByText('No workers have run for this build yet.')).toBeTruthy()
    expect(container.querySelector('[data-slot="conductor-group-other"]')?.textContent).toContain('impl41')
  })

  it('keeps ids and absolute times in Build details only', async () => {
    $conductorBuildBySession.set({ 'session-1': build })
    const { container } = render(<ConductorPane sessionId="session-1" />)
    await flush()

    fireEvent.click(screen.getByRole('button', { name: /Build details/ }))
    const details = container.querySelector('[data-slot="conductor-build-details"]') as HTMLElement

    expect(details.textContent).toContain('484496…c677d27')
    expect(details.textContent).toContain('20260924_200208_c68a80')
    expect(details.textContent).toContain('OVERNIGHT-HERMES-WORKER-2026-09-26')
    expect(details.textContent).toMatch(/Expired /)
  })

  it('shows the empty state when no build is armed', async () => {
    $conductorBuildBySession.set({ 'session-1': null })
    render(<ConductorPane sessionId="session-1" />)
    await flush()

    expect(
      screen.getByText('No build is armed in this workspace. Start one with /tb-build in a conductor session.')
    ).toBeTruthy()
    expect(request).not.toHaveBeenCalled()
  })
})
