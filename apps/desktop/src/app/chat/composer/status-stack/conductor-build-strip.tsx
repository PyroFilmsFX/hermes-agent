import type { MouseEvent } from 'react'

import { StatusChip } from '@/components/chat/status-chip'
import { WaveTrack } from '@/components/chat/wave-track'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { statusTone } from '@/lib/conductor-seat'
import { idleSentence, leaseExpiredSentence, markerStaleSentence } from '@/lib/conductor-time'
import { planTitle } from '@/lib/plan-title'
import type { ConductorBuild } from '@/store/conductor-build'

interface ConductorBuildStripProps {
  build: ConductorBuild | null
  onOpen: () => void
}

export const buildChipTip = (build: ConductorBuild) =>
  build.state === 'waiting' && build.waiting_on
    ? build.waiting_on
    : build.state === 'lease_expired'
      ? leaseExpiredSentence(build.lease_expires_at)
      : undefined

/** One glance: which build, how far, what state, and whether anyone is working. */
export function ConductorBuildStrip({ build, onOpen }: ConductorBuildStripProps) {
  if (!build) {
    return null
  }

  const running = build.lanes_running
  const stale = build.lanes_stale
  const idleCopy = build.state === 'active' || build.state === 'waiting'
  const runningClause = running > 0 ? `${running} running` : stale > 0 ? '' : idleCopy ? 'nothing running' : ''
  const staleClause = stale > 0 ? `${stale} not responding` : ''
  const idle = build.state === 'idle'
  const markerHint = idle ? null : markerStaleSentence(build.marker_stale_since)

  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    onOpen()
  }

  return (
    <div className="@container min-w-0">
      <button
        className="grid h-7 w-full grid-cols-[auto_minmax(0,1fr)_auto_auto_auto_auto] items-center gap-2.5 px-2.5 text-left text-xs hover:bg-(--ui-row-hover-background)"
        data-slot="conductor-build-strip"
        data-state={build.state}
        onClick={onClick}
        type="button"
      >
        <Tip label="Conductor build">
          <Codicon className="text-(--ui-text-tertiary)" name="layers" size="0.8rem" />
        </Tip>
        <Tip label={build.plan}>
          <span className="min-w-0 truncate text-xs text-(--ui-text-primary)">{planTitle(build.plan)}</span>
        </Tip>
        <span className="hidden @min-[340px]:flex">
          <WaveTrack
            current={build.wave_current}
            done={build.waves_done}
            tone={statusTone(build.state)}
            total={build.waves_total}
          />
        </span>
        <StatusChip
          label={idle ? idleSentence(build.idle_since) : undefined}
          status={build.state}
          tip={buildChipTip(build) ?? markerHint ?? undefined}
        />
        <span
          className="hidden min-w-0 items-center gap-1 whitespace-nowrap text-(--ui-text-tertiary) @min-[420px]:flex"
          data-slot="conductor-build-count"
        >
          {runningClause}
          {staleClause && (
            <>
              {runningClause && ', '}
              <Codicon className="text-(--ui-yellow)" name="warning" size="0.7rem" />
              {staleClause}
            </>
          )}
          {markerHint && (
            <span className="hidden @min-[560px]:inline" data-slot="conductor-build-marker-hint">
              {`${runningClause || staleClause ? ' · ' : ''}${markerHint}`}
            </span>
          )}
        </span>
        <Codicon className="text-(--ui-text-quaternary)" name="chevron-right" size="0.75rem" />
      </button>
    </div>
  )
}
