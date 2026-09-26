import { afterEach, describe, expect, it } from 'vitest'

import {
  $conductorBuildBySession,
  reconcileConductorBuildSnapshot,
  type ConductorBuild
} from './conductor-build'

const build: ConductorBuild = {
  armed_at: '2026-09-26T10:00:00Z',
  lanes_build_matched: 2,
  lanes_running: 3,
  lanes_stale: 1,
  lease_expires_at: 1_790_000_000,
  plan: 'PLAN.md',
  run_id: 'run-1',
  session_id: 'session-1',
  stage: null,
  state: 'waiting',
  unit_id: null,
  usd: null,
  usd_source: 'missing',
  wait_since: 1_790_000_000,
  waiting_on: 'worker response',
  wave_current: 3,
  waves_done: 2,
  waves_total: 7
}

afterEach(() => $conductorBuildBySession.set({}))

describe('conductor build store', () => {
  it('maps a readable snapshot to its owning session', () => {
    reconcileConductorBuildSnapshot('session-1', { build, unreadable: false })
    expect($conductorBuildBySession.get()['session-1']).toEqual(build)
  })

  it('keeps the last snapshot when the marker read is unreadable', () => {
    reconcileConductorBuildSnapshot('session-1', { build, unreadable: false })
    reconcileConductorBuildSnapshot('session-1', { build: null, unreadable: true })
    expect($conductorBuildBySession.get()['session-1']).toEqual(build)
  })

  it('clears an absent build and does not retain missing cost as zero', () => {
    reconcileConductorBuildSnapshot('session-1', { build, unreadable: false })
    reconcileConductorBuildSnapshot('session-1', { build: null, unreadable: false })
    expect($conductorBuildBySession.get()['session-1']).toBeNull()
    expect(build.usd).toBeNull()
  })
})
