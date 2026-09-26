import type { MouseEvent } from 'react'

import type { ConductorBuild } from '@/store/conductor-build'

interface ConductorBuildStripProps {
  build: ConductorBuild | null
  onOpen: () => void
}

const buildStateLabel = (state: ConductorBuild['state']) => state === 'lease_expired' ? 'lease expired' : state

export function ConductorBuildStrip({ build, onOpen }: ConductorBuildStripProps) {
  if (!build) {
    return null
  }

  const dimmed = build.state === 'blocked' || build.state === 'lease_expired'
  const hasWave = build.wave_current !== null
    && build.waves_total !== null
    && Number.isInteger(build.wave_current)
    && Number.isInteger(build.waves_total)
    && build.waves_total > 0
  const waveLabel = hasWave
    ? `W${build.wave_current}/${build.waves_total} · `
    : ''
  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    onOpen()
  }

  return (
    <button
      className={`w-full truncate px-3 py-1.5 text-left text-xs ${dimmed ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-secondary)'}`}
      data-slot="conductor-build-strip"
      data-state={dimmed ? 'dimmed' : build.state}
      onClick={onClick}
      type="button"
    >
      {`tb-build · ${build.plan} · ${waveLabel}${buildStateLabel(build.state)} · ${build.lanes_running} lanes`}
    </button>
  )
}
