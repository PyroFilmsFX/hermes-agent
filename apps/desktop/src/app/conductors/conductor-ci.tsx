// #49 R7: where a row's CI/PR comes from. Conductor first (a fresh `ci[]`
// entry costs no gh call), then the shared PR cache joined by (repo, branch),
// then a run-id status. Reads happen only while the pane is visible.

import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import type { ConductorCiObservation, ConductorRow } from '@/api/conductors'
import { useI18n } from '@/i18n'
import { fmtClock } from '@/lib/time'
import {
  $ghHealth,
  $ghRateLimit,
  branchPrKey,
  GH_RATE_FLOOR,
  isTrunkBranch,
  refreshPullRequests,
  refreshRunStatuses,
  resumeGhReads
} from '@/store/pull-requests'
import { $sessions } from '@/store/session'

/** A conductor observation this young is shown as is; no gh call for that ref. */
export const CI_FRESH_MS = 10 * 60_000

export type ChecksGlyph = 'fail' | 'none' | 'pass' | 'pending'

export function isFreshObservation(obs: ConductorCiObservation | undefined, now: number): boolean {
  if (!obs) {
    return false
  }

  const at = Date.parse(obs.checked_at)

  return Number.isFinite(at) && now - at <= CI_FRESH_MS && at - now < CI_FRESH_MS
}

export function checksGlyph(state: null | string | undefined): ChecksGlyph {
  switch ((state ?? '').toUpperCase()) {
    case 'SUCCESS':
      return 'pass'

    case 'FAILURE':
    case 'ERROR':
      return 'fail'

    case 'PENDING':
    case 'EXPECTED':
      return 'pending'

    default:
      return 'none'
  }
}

export function runGlyph(status: string, conclusion: null | string): ChecksGlyph {
  if (status !== 'completed') {
    return 'pending'
  }

  return conclusion === 'success' || conclusion === 'skipped' || conclusion === 'neutral' ? 'pass' : 'fail'
}

/** The row's repo root, from its own Hermes session (the roster response carries no
 *  absolute path, design §9). Null when the session isn't known to this window. */
export function rowRepoRoot(row: ConductorRow): null | string {
  const sid = row.orchestrator.hermes_session_id

  if (!sid) {
    return null
  }

  return $sessions.get().find(session => session.id === sid)?.git_repo_root || null
}

/** The branch to ask GitHub about; never a trunk branch. */
export function rowBranch(row: ConductorRow): null | string {
  const branch = row.build.ci[0]?.branch || row.project.branch

  return branch && !isTrunkBranch(branch) ? branch : null
}

/** A run id the conductor is waiting on (marker gate `ci_ref`). */
export function rowRunId(row: ConductorRow): null | string {
  const gate = row.build.gates.find(g => g.kind === 'ci' && g.state === 'waiting' && g.ref && /^\d+$/.test(g.ref))

  return gate?.ref ?? null
}

interface GhAsk {
  branches: Map<string, Set<string>>
  runs: Map<string, Set<string>>
}

/** Which repos/branches/runs need gh, skipping every row a fresh conductor
 *  observation already covers. */
export function collectGhAsks(rows: readonly ConductorRow[], now: number): GhAsk {
  const ask: GhAsk = { branches: new Map(), runs: new Map() }

  for (const row of rows) {
    if (isFreshObservation(row.build.ci[0], now)) {
      continue
    }

    const root = rowRepoRoot(row)

    if (!root) {
      continue
    }

    const branch = rowBranch(row)
    const run = rowRunId(row)

    if (branch) {
      ask.branches.set(root, (ask.branches.get(root) ?? new Set()).add(branch))
    }

    if (run) {
      ask.runs.set(root, (ask.runs.get(root) ?? new Set()).add(run))
    }
  }

  return ask
}

/** Pane-level poller for the CI/PR column. Hidden means no calls at all. */
export function useConductorGhReads(rows: readonly ConductorRow[], visible: boolean, pollTick: null | number): void {
  // A stable key: a poll that changed nothing about who we'd ask keeps the timer.
  const askKey = useMemo(() => {
    const ask = collectGhAsks(rows, Date.now())

    return JSON.stringify({
      b: [...ask.branches].map(([root, set]) => [root, [...set].sort()]).sort(),
      r: [...ask.runs].map(([root, set]) => [root, [...set].sort()]).sort()
    })
  }, [rows])

  useEffect(() => {
    if (!visible) {
      return
    }

    const parsed = JSON.parse(askKey) as { b: [string, string[]][]; r: [string, string[]][] }

    const read = () => {
      const lookups = Object.fromEntries(parsed.b)

      if (Object.keys(lookups).length > 0) {
        void refreshPullRequests(lookups, false, { withChecks: true })
      }

      for (const [root, ids] of parsed.r) {
        void refreshRunStatuses(root, ids)
      }
    }

    // No timer of its own: each pane poll (pollTick) re-runs this, and the
    // store's 60 s per-repo throttle decides whether gh is actually asked.
    read()
    // gh signed out stops the reads until focus; focus is the retry.
    const onFocus = () => resumeGhReads()
    window.addEventListener('focus', onFocus)

    return () => {
      window.removeEventListener('focus', onFocus)
    }
  }, [askKey, pollTick, visible])
}

export { branchPrKey }

/** Header pill: shown while the GitHub budget is under the floor, or main says
 *  we're rate limited. */
export function GhLimitPill() {
  const { t } = useI18n()
  const limit = useStore($ghRateLimit)
  const health = useStore($ghHealth)
  const low = limit !== null && limit.remaining < GH_RATE_FLOOR

  if (!low && health.error !== 'rate_limited') {
    return null
  }

  const resetAt = limit ? Date.parse(limit.resetAt) : NaN

  return (
    <span
      className="shrink-0 rounded-sm bg-(--ui-yellow)/12 px-1.5 py-0.5 text-[0.625rem] text-(--ui-yellow)"
      data-slot="conductors-gh-limit"
      title={t.conductors.ghLimitHint}
    >
      {Number.isFinite(resetAt) ? t.conductors.ghLimit(fmtClock.format(resetAt)) : t.conductors.ghLimitHint}
    </span>
  )
}
