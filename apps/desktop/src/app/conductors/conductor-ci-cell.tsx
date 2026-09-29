import { useStore } from '@nanostores/react'

import type { ConductorCiObservation, ConductorRow } from '@/api/conductors'
import { PrTag } from '@/app/chat/pr-tag'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $ghHealth, $prChecksByBranch, $pullRequestsByBranch, $runStatusById, branchPrKey } from '@/store/pull-requests'

import {
  type ChecksGlyph,
  checksGlyph,
  isFreshObservation,
  rowBranch,
  rowRepoRoot,
  rowRunId,
  runGlyph
} from './conductor-ci'

const GLYPH: Record<ChecksGlyph, string> = { fail: '✕', none: '–', pass: '✓', pending: '·' }

const GLYPH_TONE: Record<ChecksGlyph, string> = {
  fail: 'text-(--ui-red)',
  none: 'text-(--ui-text-quaternary)',
  pass: 'text-(--ui-green)',
  pending: 'text-(--ui-text-tertiary)'
}

function observationGlyph(state: string): string {
  return state === 'success' ? '✓' : state === 'failure' ? '✕' : state === 'pending' ? '·' : '?'
}

function Observation({ obs }: { obs: ConductorCiObservation }) {
  return (
    <span
      className={cn('truncate tabular-nums', obs.state === 'failure' && 'text-(--ui-red)')}
      data-ci-source="conductor"
      data-ci-state={obs.state}
    >
      {obs.pr !== null ? `#${obs.pr}` : obs.kind || 'run'} {observationGlyph(obs.state)}
    </span>
  )
}

/** CI/PR cell (§7): a fresh conductor observation as is; otherwise the shared
 *  PR cache (state chip + checks glyph) or a run-id status; a stale
 *  observation only when gh has nothing; "—" when gh isn't signed in. */
export function ConductorCiCell({ row }: { row: ConductorRow }) {
  const { t } = useI18n()
  const c = t.conductors
  const prs = useStore($pullRequestsByBranch)
  const checks = useStore($prChecksByBranch)
  const runs = useStore($runStatusById)
  const health = useStore($ghHealth)
  const obs = row.build.ci[0]

  if (isFreshObservation(obs, Date.now())) {
    return <Observation obs={obs} />
  }

  const root = rowRepoRoot(row)
  const branch = rowBranch(row)
  const key = root && branch ? branchPrKey(root, branch) : null
  const pr = key ? prs[key] : undefined
  const runId = rowRunId(row)
  const run = runId ? runs[runId] : undefined

  if (pr) {
    const glyph = checksGlyph(checks[key!] ?? pr.checks_state)

    return (
      <span className="flex min-w-0 items-center gap-1" data-ci-source="pr">
        <PrTag pr={pr} />
        <Tip label={c.ciChecks[glyph]}>
          <span className={cn('shrink-0', GLYPH_TONE[glyph])} data-checks={glyph}>
            {GLYPH[glyph]}
          </span>
        </Tip>
      </span>
    )
  }

  if (run) {
    const glyph = runGlyph(run.status, run.conclusion)

    return (
      <span className="flex min-w-0 items-center gap-1 tabular-nums" data-ci-source="run">
        <span className="truncate">run</span>
        <span className={cn('shrink-0', GLYPH_TONE[glyph])} data-checks={glyph}>
          {GLYPH[glyph]}
        </span>
      </span>
    )
  }

  if (obs) {
    return <Observation obs={obs} />
  }

  if (health.unavailable && root && (branch || runId)) {
    return (
      <Tip label={c.ghSignedOut}>
        <span className="text-(--ui-text-quaternary)" data-ci-source="unavailable">
          —
        </span>
      </Tip>
    )
  }

  return <span className="text-(--ui-text-quaternary)">{c.none}</span>
}
