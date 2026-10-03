import type { MouseEvent } from 'react'

import { openConductorsPane } from '@/app/conductors/pane-state'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import type { ConductorBuild } from '@/store/conductor-build'

interface ConductorBuildStripProps {
  build: ConductorBuild | null
  onOpen: () => void
}

const buildStateLabel = (state: ConductorBuild['state']) => {
  if (state === 'lease_expired') {
    return 'lease expired'
  }

  if (state === 'stale') {
    return 'Stale build (not marked done)'
  }

  return state
}

export function ConductorBuildStrip({ build, onOpen }: ConductorBuildStripProps) {
  if (!build) {
    return null
  }

  const dimmed = build.state === 'blocked' || build.state === 'lease_expired' || build.state === 'stale'

  const hasWave =
    build.wave_current !== null &&
    build.waves_total !== null &&
    Number.isInteger(build.wave_current) &&
    Number.isInteger(build.waves_total) &&
    build.waves_total > 0

  const waveLabel = hasWave ? `W${build.wave_current}/${build.waves_total} · ` : ''

  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    onOpen()
  }

  return (
    <div className="flex w-full min-w-0 items-center">
      <button
        className={`min-w-0 flex-1 truncate px-3 py-1.5 text-left text-xs ${dimmed ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-secondary)'}`}
        data-slot="conductor-build-strip"
        data-state={dimmed ? 'dimmed' : build.state}
        onClick={onClick}
        type="button"
      >
        {`tb-build · ${build.plan} · ${waveLabel}${buildStateLabel(build.state)} · ${build.lanes_running} lanes`}
      </button>
      <ViewAllConductors />
    </div>
  )
}

/** Overflow door onto the all-builds Conductors page (#49). */
function ViewAllConductors() {
  const { t } = useI18n()
  const label = t.conductors.viewAll

  return (
    <Tip label={label}>
      <button
        aria-label={label}
        className="mr-1.5 grid size-5 shrink-0 place-items-center rounded text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary)"
        data-slot="conductor-build-strip-view-all"
        onClick={event => {
          event.preventDefault()
          openConductorsPane()
        }}
        type="button"
      >
        <Codicon name="list-unordered" size="0.75rem" />
      </button>
    </Tip>
  )
}
