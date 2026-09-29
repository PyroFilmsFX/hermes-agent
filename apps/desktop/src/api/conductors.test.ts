import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  hermesApi: vi.fn(),
  profileScoped: vi.fn(() => ({}))
}))

const client = await import('./client')
const {
  fetchConductors,
  parseConductorsResponse,
  CONDUCTORS_SCHEMA_V1
} = await import('./conductors')

const hermesApi = vi.mocked(client.hermesApi)

const sampleSection6Example = {
  schema: 'hermes-conductors/v1',
  generated_at: 1790670000.0,
  rows: [
    {
      key: 'sha256(marker_path)[:16]',
      orchestrator: {
        hermes_session_id: null,
        profile: 'default',
        title: null,
        role: 'orchestrator',
        attribution: 'live',
        claude_sid_short: '6d739ad6',
        live: 'busy'
      },
      project: {
        name: 'hermes-cntrl',
        root_display: '~/Documents/…/hermes-cntrl',
        branch: 'cntrl-hermes-worker',
        bound: false,
        nested_in: null
      },
      build: {
        run_id: 'cc546d7b99dc45d6a829da8e2c77a673',
        build_id: null,
        plan_title: 'Hermes worker plan b9/b10',
        armed_at: '2026-09-29T06:39:40Z',
        marker_state: 'active',
        phase: 'build',
        waves: { done: 1, total: 4, current: 2 },
        units: { done: 7, running: 3, failed: 0, remaining: 12, total: 22 },
        current_units: [
          { id: 'H5', title: 'attest.py verifier', seat: 'agy', since: '2026-09-29T07:12:03Z' }
        ],
        remaining_waves: [
          { index: 3, id: 'W3', title: '#49 Conductors page', units: 19 }
        ],
        estimate: {
          unit: 'work_hours',
          p50: 6.5,
          p90: 10.0,
          basis: 'reforecast',
          as_of: '2026-09-29T07:12:03Z',
          prediction_id: null
        },
        gates: [
          {
            id: 'g-7209',
            kind: 'ci',
            label: 'CI run 36533732367 on sync/land-b9',
            since: '2026-09-29T07:12:03Z',
            due: null,
            ref: '36533732367',
            state: 'waiting',
            waiter: true
          }
        ],
        seats: {
          agy: { spawned: 12, running: 2, succeeded: 8, failed: 1, refused: 3 }
        },
        refusals: [
          {
            seat: 'agy',
            code: 'stale_daemon',
            count: 3,
            last_at: '2026-09-29T07:12:03Z',
            note: 'tb-workers daemon predates 3.61.10'
          }
        ],
        lanes: { running: 3, stale: 0, cap: 6 },
        ci: [
          {
            kind: 'run',
            ref: '36533732367',
            branch: 'sync/land-b9',
            pr: null,
            state: 'pending',
            url: 'https://github.com/org/repo/actions/runs/36533732367',
            checked_at: '2026-09-29T07:12:03Z'
          }
        ],
        owner_blockers: [
          {
            id: 'ob-1',
            action: 'approve',
            label: 'Reinstall owner verifier (admin prompt)',
            since: '2026-09-29T07:12:03Z',
            ref: null
          }
        ],
        last_activity_at: 1790670000.0,
        liveness: 'active',
        idle_since: null,
        blocked: false
      },
      record: {
        present: true,
        valid: true,
        reason: 'ok',
        fresh: true,
        status_at: 1790670000.0,
        seq: 42
      },
      field_sources: {
        estimate: 'status',
        seats: 'status',
        gates: 'status'
      },
      other_builds: [
        {
          run_id: 'other-1',
          plan_title: 'Other plan',
          liveness: 'idle',
          waves: { done: 0, total: 2, current: 1 }
        }
      ]
    }
  ],
  sources: {
    marker_index: 'ok',
    state_dirs: 6,
    skipped: 0
  },
  abandoned: 1
}

describe('parseConductorsResponse', () => {
  it('accepts the §6 example exactly', () => {
    const parsed = parseConductorsResponse(sampleSection6Example)
    expect(parsed.schema).toBe(CONDUCTORS_SCHEMA_V1)
    expect(parsed.generated_at).toBe(1790670000.0)
    expect(parsed.abandoned).toBe(1)
    expect(parsed.sources).toEqual({
      marker_index: 'ok',
      state_dirs: 6,
      skipped: 0
    })
    expect(parsed.rows).toHaveLength(1)

    const row = parsed.rows[0]
    expect(row.key).toBe('sha256(marker_path)[:16]')
    expect(row.orchestrator).toEqual({
      hermes_session_id: null,
      profile: 'default',
      title: null,
      role: 'orchestrator',
      attribution: 'live',
      claude_sid_short: '6d739ad6',
      live: 'busy'
    })
    expect(row.project).toEqual({
      name: 'hermes-cntrl',
      root_display: '~/Documents/…/hermes-cntrl',
      branch: 'cntrl-hermes-worker',
      bound: false,
      nested_in: null
    })
    expect(row.build.run_id).toBe('cc546d7b99dc45d6a829da8e2c77a673')
    expect(row.build.phase).toBe('build')
    expect(row.build.waves).toEqual({ done: 1, total: 4, current: 2 })
    expect(row.build.units).toEqual({ done: 7, running: 3, failed: 0, remaining: 12, total: 22 })
    expect(row.build.current_units).toEqual([
      { id: 'H5', title: 'attest.py verifier', seat: 'agy', since: '2026-09-29T07:12:03Z' }
    ])
    expect(row.build.remaining_waves).toEqual([
      { index: 3, id: 'W3', title: '#49 Conductors page', units: 19 }
    ])
    expect(row.build.estimate).toEqual({
      unit: 'work_hours',
      p50: 6.5,
      p90: 10.0,
      basis: 'reforecast',
      as_of: '2026-09-29T07:12:03Z',
      prediction_id: null
    })
    expect(row.build.gates).toEqual([
      {
        id: 'g-7209',
        kind: 'ci',
        label: 'CI run 36533732367 on sync/land-b9',
        since: '2026-09-29T07:12:03Z',
        due: null,
        ref: '36533732367',
        state: 'waiting',
        waiter: true
      }
    ])
    expect(row.build.seats).toEqual({
      agy: { spawned: 12, running: 2, succeeded: 8, failed: 1, refused: 3 }
    })
    expect(row.build.refusals).toEqual([
      {
        seat: 'agy',
        code: 'stale_daemon',
        count: 3,
        last_at: '2026-09-29T07:12:03Z',
        note: 'tb-workers daemon predates 3.61.10'
      }
    ])
    expect(row.build.lanes).toEqual({ running: 3, stale: 0, cap: 6 })
    expect(row.build.ci).toEqual([
      {
        kind: 'run',
        ref: '36533732367',
        branch: 'sync/land-b9',
        pr: null,
        state: 'pending',
        url: 'https://github.com/org/repo/actions/runs/36533732367',
        checked_at: '2026-09-29T07:12:03Z'
      }
    ])
    expect(row.build.owner_blockers).toEqual([
      {
        id: 'ob-1',
        action: 'approve',
        label: 'Reinstall owner verifier (admin prompt)',
        since: '2026-09-29T07:12:03Z',
        ref: null
      }
    ])
    expect(row.build.last_activity_at).toBe(1790670000.0)
    expect(row.build.liveness).toBe('active')
    expect(row.build.idle_since).toBeNull()
    expect(row.build.blocked).toBe(false)
    expect(row.record).toEqual({
      present: true,
      valid: true,
      reason: 'ok',
      fresh: true,
      status_at: 1790670000.0,
      seq: 42
    })
    expect(row.field_sources).toEqual({
      estimate: 'status',
      seats: 'status',
      gates: 'status'
    })
    expect(row.other_builds).toEqual([
      {
        run_id: 'other-1',
        plan_title: 'Other plan',
        liveness: 'idle',
        waves: { done: 0, total: 2, current: 1 }
      }
    ])
  })

  it('rejects a response with a wrong or missing schema', () => {
    expect(() => parseConductorsResponse(null)).toThrow('expected object')
    expect(() => parseConductorsResponse({ schema: 'hermes-conductors/v2' })).toThrow(
      'Invalid conductors response schema: expected "hermes-conductors/v1", got "hermes-conductors/v2"'
    )
    expect(() => parseConductorsResponse({ schema: 'other' })).toThrow('Invalid conductors response schema')
    expect(() => parseConductorsResponse({})).toThrow('Invalid conductors response schema')
  })

  it('ignores unknown extra fields in the payload', () => {
    const withExtra = {
      ...sampleSection6Example,
      unknown_root: 'extra',
      rows: [
        {
          ...sampleSection6Example.rows[0],
          unknown_row_field: 42,
          orchestrator: {
            ...sampleSection6Example.rows[0].orchestrator,
            unknown_orch: true
          },
          build: {
            ...sampleSection6Example.rows[0].build,
            unknown_build_field: 'strip_me'
          }
        }
      ]
    }
    const parsed = parseConductorsResponse(withExtra)
    expect(parsed).not.toHaveProperty('unknown_root')
    expect(parsed.rows[0]).not.toHaveProperty('unknown_row_field')
    expect(parsed.rows[0].orchestrator).not.toHaveProperty('unknown_orch')
    expect(parsed.rows[0].build).not.toHaveProperty('unknown_build_field')
  })
})

describe('fetchConductors', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls GET /api/profiles/conductors without fresh by default', async () => {
    hermesApi.mockResolvedValueOnce(sampleSection6Example as never)
    const result = await fetchConductors()
    expect(hermesApi).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/profiles/conductors'
      })
    )
    expect(result.schema).toBe(CONDUCTORS_SCHEMA_V1)
  })

  it('passes ?fresh=1 when fresh: true is specified', async () => {
    hermesApi.mockResolvedValueOnce(sampleSection6Example as never)
    const result = await fetchConductors({ fresh: true })
    expect(hermesApi).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/profiles/conductors?fresh=1'
      })
    )
    expect(result.schema).toBe(CONDUCTORS_SCHEMA_V1)
  })

  it('supports abort signal', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(fetchConductors({ signal: controller.signal })).rejects.toThrow()
    expect(hermesApi).not.toHaveBeenCalled()
  })
})
