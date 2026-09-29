import { hermesApi, profileScoped } from './client'

export const CONDUCTORS_SCHEMA_V1 = 'hermes-conductors/v1' as const

export type ConductorOrchestratorAttribution =
  | 'live'
  | 'bound'
  | 'stamped'
  | 'db'
  | 'workspace'
  | 'none'

export type ConductorOrchestratorLive =
  | 'busy'
  | 'attached'
  | 'cli'
  | 'none'

export interface ConductorOrchestrator {
  hermes_session_id: string | null
  profile: string | null
  title: string | null
  role: string | null
  attribution: ConductorOrchestratorAttribution
  claude_sid_short: string
  live: ConductorOrchestratorLive
}

export interface ConductorProject {
  name: string
  root_display: string
  branch: string | null
  bound: boolean
  nested_in: string | null
}

export interface ConductorWaves {
  done: number
  total: number
  current: number
}

export interface ConductorUnits {
  done: number
  running: number
  failed: number
  remaining: number
  total: number
}

export interface ConductorCurrentUnit {
  id: string
  title: string
  seat: string
  since: string
}

export interface ConductorRemainingWave {
  index: number
  id: string
  title: string
  units: number
}

export interface ConductorEstimate {
  unit: string
  p50: number
  p90: number
  basis: string
  as_of: string
  prediction_id: string | null
}

export interface ConductorGate {
  id: string
  kind: string
  label: string
  since: string
  due: string | null
  ref: string | null
  state: string
  waiter: boolean
}

export interface ConductorSeatCount {
  spawned: number
  running: number
  succeeded: number
  failed: number
  refused: number
}

export interface ConductorRefusal {
  seat: string
  code: string
  count: number
  last_at: string
  note: string
}

export interface ConductorLanes {
  running: number
  stale: number
  cap: number | null
}

export interface ConductorCiObservation {
  kind: string
  ref: string
  branch: string
  pr: number | null
  state: string
  url: string
  checked_at: string
}

export interface ConductorOwnerBlocker {
  id: string
  action: string
  label: string
  since: string
  ref: string | null
}

export type ConductorLiveness = 'active' | 'quiet' | 'idle' | 'stale'

export interface ConductorBuildInfo {
  run_id: string
  build_id: string | null
  plan_title: string | null
  armed_at: string | null
  marker_state: string
  phase: string
  waves: ConductorWaves
  units: ConductorUnits | null
  current_units: ConductorCurrentUnit[]
  remaining_waves: ConductorRemainingWave[]
  estimate: ConductorEstimate | null
  gates: ConductorGate[]
  seats: Record<string, ConductorSeatCount>
  refusals: ConductorRefusal[]
  lanes: ConductorLanes
  ci: ConductorCiObservation[]
  owner_blockers: ConductorOwnerBlocker[]
  last_activity_at: number
  liveness: ConductorLiveness
  idle_since: number | null
  blocked: boolean
}

export type ConductorRecordReason =
  | 'ok'
  | 'missing'
  | 'mismatched'
  | 'newer_schema'
  | 'unreadable'
  | 'too_large'

export interface ConductorRecord {
  present: boolean
  valid: boolean
  reason: ConductorRecordReason
  fresh: boolean
  status_at: number | null
  seq: number | null
}

export interface ConductorOtherBuild {
  run_id: string
  plan_title: string | null
  liveness: ConductorLiveness
  waves: ConductorWaves
}

export interface ConductorRow {
  key: string
  orchestrator: ConductorOrchestrator
  project: ConductorProject
  build: ConductorBuildInfo
  record: ConductorRecord
  field_sources: Record<string, string>
  other_builds: ConductorOtherBuild[]
}

export type ConductorMarkerIndexStatus = 'ok' | 'missing' | 'unreadable' | 'truncated'

export interface ConductorSources {
  marker_index: ConductorMarkerIndexStatus
  state_dirs: number
  skipped: number
}

export interface ConductorsResponse {
  schema: typeof CONDUCTORS_SCHEMA_V1
  generated_at: number
  rows: ConductorRow[]
  sources: ConductorSources
  abandoned: number
}

export interface FetchConductorsOptions {
  fresh?: boolean
  signal?: AbortSignal
}

function asObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return null
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function asNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  return fallback
}

function asNullableNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  return null
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback
}

const VALID_ATTRIBUTIONS: ReadonlySet<string> = new Set([
  'live',
  'bound',
  'stamped',
  'db',
  'workspace',
  'none'
])

const VALID_LIVES: ReadonlySet<string> = new Set([
  'busy',
  'attached',
  'cli',
  'none'
])

const VALID_LIVENESSES: ReadonlySet<string> = new Set([
  'active',
  'quiet',
  'idle',
  'stale'
])

const VALID_RECORD_REASONS: ReadonlySet<string> = new Set([
  'ok',
  'missing',
  'mismatched',
  'newer_schema',
  'unreadable',
  'too_large'
])

const VALID_MARKER_INDEX_STATUSES: ReadonlySet<string> = new Set([
  'ok',
  'missing',
  'unreadable',
  'truncated'
])

export function parseConductorsResponse(raw: unknown): ConductorsResponse {
  const root = asObject(raw)
  if (!root) {
    throw new Error('Invalid conductors response: expected object')
  }

  if (root.schema !== CONDUCTORS_SCHEMA_V1) {
    throw new Error(
      `Invalid conductors response schema: expected "${CONDUCTORS_SCHEMA_V1}", got "${String(root.schema)}"`
    )
  }

  const generated_at = asNumber(root.generated_at, Date.now() / 1000)
  const abandoned = asNumber(root.abandoned, 0)

  const rawSources = asObject(root.sources) ?? {}
  const markerIndexRaw = asString(rawSources.marker_index, 'missing')
  const marker_index: ConductorMarkerIndexStatus = VALID_MARKER_INDEX_STATUSES.has(markerIndexRaw)
    ? (markerIndexRaw as ConductorMarkerIndexStatus)
    : 'missing'

  const sources: ConductorSources = {
    marker_index,
    state_dirs: asNumber(rawSources.state_dirs, 0),
    skipped: asNumber(rawSources.skipped, 0)
  }

  const rawRows = Array.isArray(root.rows) ? root.rows : []
  const rows: ConductorRow[] = []

  for (const rawRow of rawRows) {
    const rowObj = asObject(rawRow)
    if (!rowObj) continue

    const key = asString(rowObj.key, '')

    // Orchestrator
    const orchObj = asObject(rowObj.orchestrator) ?? {}
    const attrRaw = asString(orchObj.attribution, 'none')
    const attribution: ConductorOrchestratorAttribution = VALID_ATTRIBUTIONS.has(attrRaw)
      ? (attrRaw as ConductorOrchestratorAttribution)
      : 'none'
    const liveRaw = asString(orchObj.live, 'none')
    const live: ConductorOrchestratorLive = VALID_LIVES.has(liveRaw)
      ? (liveRaw as ConductorOrchestratorLive)
      : 'none'

    const orchestrator: ConductorOrchestrator = {
      hermes_session_id: asNullableString(orchObj.hermes_session_id),
      profile: asNullableString(orchObj.profile),
      title: asNullableString(orchObj.title),
      role: asNullableString(orchObj.role),
      attribution,
      claude_sid_short: asString(orchObj.claude_sid_short, ''),
      live
    }

    // Project
    const projObj = asObject(rowObj.project) ?? {}
    const project: ConductorProject = {
      name: asString(projObj.name, ''),
      root_display: asString(projObj.root_display, ''),
      branch: asNullableString(projObj.branch),
      bound: asBoolean(projObj.bound, false),
      nested_in: asNullableString(projObj.nested_in)
    }

    // Build
    const buildObj = asObject(rowObj.build) ?? {}
    const wavesObj = asObject(buildObj.waves) ?? {}
    const waves: ConductorWaves = {
      done: asNumber(wavesObj.done, 0),
      total: asNumber(wavesObj.total, 0),
      current: asNumber(wavesObj.current, 0)
    }

    let units: ConductorUnits | null = null
    const unitsObj = asObject(buildObj.units)
    if (unitsObj) {
      units = {
        done: asNumber(unitsObj.done, 0),
        running: asNumber(unitsObj.running, 0),
        failed: asNumber(unitsObj.failed, 0),
        remaining: asNumber(unitsObj.remaining, 0),
        total: asNumber(unitsObj.total, 0)
      }
    }

    const current_units: ConductorCurrentUnit[] = []
    if (Array.isArray(buildObj.current_units)) {
      for (const item of buildObj.current_units) {
        const u = asObject(item)
        if (u) {
          current_units.push({
            id: asString(u.id, ''),
            title: asString(u.title, ''),
            seat: asString(u.seat, ''),
            since: asString(u.since, '')
          })
        }
      }
    }

    const remaining_waves: ConductorRemainingWave[] = []
    if (Array.isArray(buildObj.remaining_waves)) {
      for (const item of buildObj.remaining_waves) {
        const rw = asObject(item)
        if (rw) {
          remaining_waves.push({
            index: asNumber(rw.index, 0),
            id: asString(rw.id, ''),
            title: asString(rw.title, ''),
            units: asNumber(rw.units, 0)
          })
        }
      }
    }

    let estimate: ConductorEstimate | null = null
    const estObj = asObject(buildObj.estimate)
    if (estObj) {
      estimate = {
        unit: asString(estObj.unit, ''),
        p50: asNumber(estObj.p50, 0),
        p90: asNumber(estObj.p90, 0),
        basis: asString(estObj.basis, ''),
        as_of: asString(estObj.as_of, ''),
        prediction_id: asNullableString(estObj.prediction_id)
      }
    }

    const gates: ConductorGate[] = []
    if (Array.isArray(buildObj.gates)) {
      for (const item of buildObj.gates) {
        const g = asObject(item)
        if (g) {
          gates.push({
            id: asString(g.id, ''),
            kind: asString(g.kind, ''),
            label: asString(g.label, ''),
            since: asString(g.since, ''),
            due: asNullableString(g.due),
            ref: asNullableString(g.ref),
            state: asString(g.state, ''),
            waiter: asBoolean(g.waiter, false)
          })
        }
      }
    }

    const seats: Record<string, ConductorSeatCount> = {}
    const seatsObj = asObject(buildObj.seats) ?? {}
    for (const [seatKey, rawSeatCount] of Object.entries(seatsObj)) {
      const sc = asObject(rawSeatCount)
      if (sc) {
        seats[seatKey] = {
          spawned: asNumber(sc.spawned, 0),
          running: asNumber(sc.running, 0),
          succeeded: asNumber(sc.succeeded, 0),
          failed: asNumber(sc.failed, 0),
          refused: asNumber(sc.refused, 0)
        }
      }
    }

    const refusals: ConductorRefusal[] = []
    if (Array.isArray(buildObj.refusals)) {
      for (const item of buildObj.refusals) {
        const r = asObject(item)
        if (r) {
          refusals.push({
            seat: asString(r.seat, ''),
            code: asString(r.code, ''),
            count: asNumber(r.count, 0),
            last_at: asString(r.last_at, ''),
            note: asString(r.note, '')
          })
        }
      }
    }

    const lanesObj = asObject(buildObj.lanes) ?? {}
    const lanes: ConductorLanes = {
      running: asNumber(lanesObj.running, 0),
      stale: asNumber(lanesObj.stale, 0),
      cap: asNullableNumber(lanesObj.cap)
    }

    const ci: ConductorCiObservation[] = []
    if (Array.isArray(buildObj.ci)) {
      for (const item of buildObj.ci) {
        const c = asObject(item)
        if (c) {
          ci.push({
            kind: asString(c.kind, ''),
            ref: asString(c.ref, ''),
            branch: asString(c.branch, ''),
            pr: asNullableNumber(c.pr),
            state: asString(c.state, ''),
            url: asString(c.url, ''),
            checked_at: asString(c.checked_at, '')
          })
        }
      }
    }

    const owner_blockers: ConductorOwnerBlocker[] = []
    if (Array.isArray(buildObj.owner_blockers)) {
      for (const item of buildObj.owner_blockers) {
        const ob = asObject(item)
        if (ob) {
          owner_blockers.push({
            id: asString(ob.id, ''),
            action: asString(ob.action, ''),
            label: asString(ob.label, ''),
            since: asString(ob.since, ''),
            ref: asNullableString(ob.ref)
          })
        }
      }
    }

    const livenessRaw = asString(buildObj.liveness, 'active')
    const liveness: ConductorLiveness = VALID_LIVENESSES.has(livenessRaw)
      ? (livenessRaw as ConductorLiveness)
      : 'active'

    const build: ConductorBuildInfo = {
      run_id: asString(buildObj.run_id, ''),
      build_id: asNullableString(buildObj.build_id),
      plan_title: asNullableString(buildObj.plan_title),
      armed_at: asNullableString(buildObj.armed_at),
      marker_state: asString(buildObj.marker_state, ''),
      phase: asString(buildObj.phase, ''),
      waves,
      units,
      current_units,
      remaining_waves,
      estimate,
      gates,
      seats,
      refusals,
      lanes,
      ci,
      owner_blockers,
      last_activity_at: asNumber(buildObj.last_activity_at, 0),
      liveness,
      idle_since: asNullableNumber(buildObj.idle_since),
      blocked: asBoolean(buildObj.blocked, false)
    }

    // Record
    const recObj = asObject(rowObj.record) ?? {}
    const reasonRaw = asString(recObj.reason, 'missing')
    const reason: ConductorRecordReason = VALID_RECORD_REASONS.has(reasonRaw)
      ? (reasonRaw as ConductorRecordReason)
      : 'missing'

    const record: ConductorRecord = {
      present: asBoolean(recObj.present, false),
      valid: asBoolean(recObj.valid, false),
      reason,
      fresh: asBoolean(recObj.fresh, false),
      status_at: asNullableNumber(recObj.status_at),
      seq: asNullableNumber(recObj.seq)
    }

    // Field sources
    const field_sources: Record<string, string> = {}
    const fsObj = asObject(rowObj.field_sources) ?? {}
    for (const [k, v] of Object.entries(fsObj)) {
      if (typeof v === 'string') {
        field_sources[k] = v
      }
    }

    // Other builds
    const other_builds: ConductorOtherBuild[] = []
    if (Array.isArray(rowObj.other_builds)) {
      for (const item of rowObj.other_builds) {
        const ob = asObject(item)
        if (ob) {
          const obWavesObj = asObject(ob.waves) ?? {}
          const obLivenessRaw = asString(ob.liveness, 'active')
          const obLiveness: ConductorLiveness = VALID_LIVENESSES.has(obLivenessRaw)
            ? (obLivenessRaw as ConductorLiveness)
            : 'active'
          other_builds.push({
            run_id: asString(ob.run_id, ''),
            plan_title: asNullableString(ob.plan_title),
            liveness: obLiveness,
            waves: {
              done: asNumber(obWavesObj.done, 0),
              total: asNumber(obWavesObj.total, 0),
              current: asNumber(obWavesObj.current, 0)
            }
          })
        }
      }
    }

    rows.push({
      key,
      orchestrator,
      project,
      build,
      record,
      field_sources,
      other_builds
    })
  }

  return {
    schema: CONDUCTORS_SCHEMA_V1,
    generated_at,
    rows,
    sources,
    abandoned
  }
}

export async function fetchConductors(options: FetchConductorsOptions = {}): Promise<ConductorsResponse> {
  const query = options.fresh ? '?fresh=1' : ''
  const path = `/api/profiles/conductors${query}`

  options.signal?.throwIfAborted()

  const request = hermesApi<unknown>({
    ...profileScoped(),
    path,
    timeoutMs: 30_000
  })

  if (!options.signal) {
    return request.then(parseConductorsResponse)
  }

  const signal = options.signal
  return new Promise<ConductorsResponse>((resolve, reject) => {
    const onAbort = () => reject(signal.reason ?? new DOMException('The operation was aborted', 'AbortError'))
    signal.addEventListener('abort', onAbort, { once: true })
    request.then(
      result => {
        signal.removeEventListener('abort', onAbort)
        try {
          signal.throwIfAborted()
          resolve(parseConductorsResponse(result))
        } catch (err) {
          reject(err)
        }
      },
      error => {
        signal.removeEventListener('abort', onAbort)
        reject(error)
      }
    )
  })
}
