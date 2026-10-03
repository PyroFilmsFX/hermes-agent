// Test fixtures for the Conductors pane: one fully-reported row that tests
// override per case. Kept out of the production import graph.
import type { ConductorRow, ConductorsResponse } from '@/api/conductors'

export const GENERATED_AT = 1_790_670_000

type RowOverrides = Omit<Partial<ConductorRow>, 'build' | 'orchestrator' | 'project' | 'record'> & {
  build?: Partial<ConductorRow['build']>
  orchestrator?: Partial<ConductorRow['orchestrator']>
  project?: Partial<ConductorRow['project']>
  record?: Partial<ConductorRow['record']>
}

export function makeConductorRow(key: string, overrides: RowOverrides = {}): ConductorRow {
  const base: ConductorRow = {
    key,
    orchestrator: {
      hermes_session_id: `hs-${key}`,
      profile: 'default',
      title: `Session ${key}`,
      role: 'orchestrator',
      attribution: 'live',
      claude_sid_short: '6d739ad6',
      live: 'busy'
    },
    project: { name: `project-${key}`, root_display: '~/p', branch: 'main', bound: false, nested_in: null },
    build: {
      run_id: `run-${key}`,
      build_id: null,
      plan_title: 'Plan',
      armed_at: null,
      marker_state: 'running',
      phase: 'build',
      waves: { done: 1, total: 4, current: 2 },
      units: { done: 7, running: 3, failed: 0, remaining: 12, total: 22 },
      current_units: [{ id: 'H5', title: 'attest.py verifier', seat: 'agy', since: '2026-09-29T07:00:00Z' }],
      remaining_waves: [],
      estimate: { unit: 'work_hours', p50: 6.5, p90: 10, basis: 'reforecast', as_of: '', prediction_id: null },
      gates: [
        {
          id: 'g1',
          kind: 'ci',
          label: 'CI run',
          since: '2026-09-29T07:12:00Z',
          due: null,
          ref: null,
          state: 'waiting',
          waiter: true
        }
      ],
      seats: { agy: { spawned: 2, running: 1, succeeded: 1, failed: 0, refused: 0 } },
      refusals: [],
      lanes: { running: 3, stale: 0, cap: 6 },
      ci: [],
      owner_blockers: [],
      last_activity_at: GENERATED_AT - 240,
      liveness: 'active',
      idle_since: null,
      blocked: false
    },
    record: { present: true, valid: true, reason: 'ok', fresh: true, status_at: GENERATED_AT - 60, seq: 3 },
    field_sources: {
      progress: 'status',
      waves: 'status',
      estimate: 'status',
      gates: 'status',
      seats: 'status',
      lanes: 'status',
      ci: 'status',
      owner_blockers: 'status',
      last_activity: 'status'
    },
    other_builds: []
  }

  return {
    ...base,
    ...overrides,
    orchestrator: { ...base.orchestrator, ...overrides.orchestrator },
    project: { ...base.project, ...overrides.project },
    build: { ...base.build, ...overrides.build },
    record: { ...base.record, ...overrides.record },
    field_sources: overrides.field_sources ?? base.field_sources
  }
}

export function makeResponse(rows: ConductorRow[], extra: Partial<ConductorsResponse> = {}): ConductorsResponse {
  return {
    schema: 'hermes-conductors/v1',
    generated_at: GENERATED_AT,
    rows,
    sources: { marker_index: 'ok', state_dirs: 1, skipped: 0 },
    abandoned: 0,
    ...extra
  }
}

/** Every field the backend can derive, marked as derived. */
export const ALL_DERIVED: Record<string, string> = {
  progress: 'derived',
  waves: 'derived',
  estimate: 'derived',
  gates: 'derived',
  seats: 'derived',
  lanes: 'derived',
  ci: 'derived',
  owner_blockers: 'derived',
  last_activity: 'derived',
  phase: 'derived'
}
