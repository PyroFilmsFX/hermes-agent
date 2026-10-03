// Pure view-model helpers for the Conductors pane (#49 R3/R5). Nothing here
// touches the DOM, a store or a timer, so the pane and its rows stay cheap to
// render and the rules below are unit-testable on their own.

import type { ConductorLiveness, ConductorRow } from '@/api/conductors'
import { DAY, fmtClock } from '@/lib/time'

/** The chip's state: the backend's §4 liveness, plus the renderer-side
 *  "abandoned" hide rule (owner gone and lease expired over 7 days ago). */
export type ConductorChipState = ConductorLiveness | 'abandoned'

export type ConductorFilter = 'active' | 'all' | 'idleStale' | 'needsMe'

/** §4 hide rule (PA `_MARKER_ABANDONED_SECONDS`): 7 days past lease expiry. */
export const ABANDONED_AFTER_SECONDS = (7 * DAY) / 1000

/** A row moves behind "Show abandoned (n)" when its owner is not live and its
 *  lease ran out more than 7 days before the response was generated. It is
 *  never dropped. Measured against `generated_at`, not the ticking clock, so a
 *  row can't jump groups between two polls of identical data. */
export function isAbandonedRow(row: ConductorRow, generatedAtSec: number): boolean {
  // The backend owns the rule; the local check is only for older backends.
  if (typeof row.abandoned === 'boolean') {
    return row.abandoned
  }

  const { build, orchestrator } = row

  if (orchestrator.live !== 'none') {
    return false
  }

  if (build.liveness !== 'idle' && build.liveness !== 'stale') {
    return false
  }

  return build.idle_since !== null && generatedAtSec - build.idle_since > ABANDONED_AFTER_SECONDS
}

export function chipStateOf(row: ConductorRow, abandoned: boolean): ConductorChipState {
  return abandoned ? 'abandoned' : row.build.liveness
}

/** Blocked is a badge, not a liveness value: blocked + active is possible. */
export function needsOwner(row: ConductorRow): boolean {
  return row.build.blocked || row.build.owner_blockers.length > 0
}

export function hasFailingCi(row: ConductorRow): boolean {
  return (
    row.build.ci.some(obs => obs.state === 'failure') ||
    row.build.gates.some(gate => gate.kind === 'ci' && gate.state === 'failed')
  )
}

// §8 sort: owner-blocked, failing CI, stale, idle, active, quiet.
const LIVENESS_RANK: Record<ConductorLiveness, number> = { stale: 2, idle: 3, active: 4, quiet: 5 }

function sortRank(row: ConductorRow): number {
  if (needsOwner(row)) {
    return 0
  }

  if (hasFailingCi(row)) {
    return 1
  }

  return LIVENESS_RANK[row.build.liveness]
}

/** Stable §8 order; within a group, newest activity first, then key. */
export function sortConductorRows(rows: readonly ConductorRow[]): ConductorRow[] {
  return [...rows].sort(
    (a, b) =>
      sortRank(a) - sortRank(b) ||
      b.build.last_activity_at - a.build.last_activity_at ||
      (a.key < b.key ? -1 : a.key > b.key ? 1 : 0)
  )
}

export function matchesFilter(row: ConductorRow, filter: ConductorFilter): boolean {
  switch (filter) {
    case 'needsMe':
      return needsOwner(row)

    case 'active':
      return row.build.liveness === 'active' || row.build.liveness === 'quiet'

    case 'idleStale':
      return row.build.liveness === 'idle' || row.build.liveness === 'stale'

    default:
      return true
  }
}

/** Columns that can carry the `≈` provenance mark (§3.6 "Marked" column), and
 *  the `field_sources` key each reads. Wave, estimate, CI/PR and last activity
 *  are never marked. */
export const DERIVABLE_FIELD = {
  blockers: 'owner_blockers',
  gates: 'gates',
  lanes: 'lanes',
  now: 'progress',
  remaining: 'progress',
  seats: 'seats'
} as const

export type DerivableColumn = keyof typeof DERIVABLE_FIELD

/** True only when the backend says Hermes derived the field. An absent entry
 *  claims nothing (no mark); `status` is the conductor's own value; `none`
 *  means there was nothing to show. */
export function isDerivedColumn(row: ConductorRow, column: DerivableColumn): boolean {
  const source = row.field_sources[DERIVABLE_FIELD[column]]

  return source !== undefined && source !== 'status' && source !== 'none'
}

/**
 * Keep a previous row object when the fresh one is structurally identical, so
 * memoised rows skip re-rendering on a poll that changed nothing for them
 * (§9). The parser builds every object in a fixed key order, so a JSON string
 * is a sound equality check for these small records.
 */
export interface RowCache {
  byKey: ReadonlyMap<string, { json: string; row: ConductorRow }>
}

export function reconcileRows(
  cache: RowCache,
  next: readonly ConductorRow[]
): { cache: RowCache; rows: ConductorRow[] } {
  const byKey = new Map<string, { json: string; row: ConductorRow }>()

  const rows = next.map(row => {
    const json = JSON.stringify(row)
    const previous = cache.byKey.get(row.key)
    const kept = previous && previous.json === json ? previous.row : row
    byKey.set(row.key, { json, row: kept })

    return kept
  })

  return { cache: { byKey }, rows }
}

export const EMPTY_ROW_CACHE: RowCache = { byKey: new Map() }

// ── Formatting ─────────────────────────────────────────────────────────────

const fmtDueDay = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', weekday: 'short' })

export function isoToMs(value: null | string | undefined): null | number {
  if (!value) {
    return null
  }

  const ms = Date.parse(value)

  return Number.isFinite(ms) ? ms : null
}

export function clockOf(ms: null | number): null | string {
  return ms === null ? null : fmtClock.format(ms)
}

export function dueDayOf(ms: null | number): null | string {
  return ms === null ? null : fmtDueDay.format(ms)
}

/** `6.5` / `10` — one decimal under 10 h, whole hours above. */
export function formatHours(value: number): string {
  if (value >= 10) {
    return String(Math.round(value))
  }

  return String(Math.round(value * 10) / 10)
}

export const GATE_KIND_LABEL: Record<string, string> = {
  ci: 'CI',
  date: 'Date',
  external: 'External',
  owner: 'Owner',
  relaunch: 'Relaunch',
  review: 'Review'
}

export function gateKindLabel(kind: string): string {
  return GATE_KIND_LABEL[kind] ?? (kind ? kind[0].toUpperCase() + kind.slice(1) : '?')
}

/** Fixed seat order and monograms (§8 column 6). `other` shows only when used. */
export const SEAT_ORDER = [
  { key: 'agy', mono: 'ag' },
  { key: 'muse', mono: 'mu' },
  { key: 'codex', mono: 'cx' },
  { key: 'sonnet', mono: 'so' },
  { key: 'opus', mono: 'op' }
] as const

/** Relay-derived seats can't see native Claude lanes: §3.6 shows "?". */
export const NATIVE_SEATS: ReadonlySet<string> = new Set(['sonnet', 'opus'])
