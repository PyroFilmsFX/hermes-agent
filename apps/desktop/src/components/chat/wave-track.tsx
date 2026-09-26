import { Progress } from '@/components/ui/progress'
import { Tip } from '@/components/ui/tooltip'
import type { StatusTone } from '@/lib/conductor-seat'
import { cn } from '@/lib/utils'

import { TONE_VAR } from './status-chip'

interface WaveTrackProps {
  className?: string
  current: number | null
  done: number
  /** The build state's tone paints the current wave. */
  tone: StatusTone
  total: number | null
}

const MAX_SEGMENTS = 12

export const waveTrackLabel = (current: number, total: number, done: number) =>
  `Wave ${current} of ${total}, ${done > 0 ? `${done} closed` : 'none closed'}`

export const hasWaveTrack = (total: number | null, current: number | null) =>
  total !== null && current !== null && Number.isInteger(total) && Number.isInteger(current) && total >= 2

/** One short segment per wave: closed green, current in the state tone, future a hairline. */
export function WaveTrack({ className, current, done, tone, total }: WaveTrackProps) {
  if (!hasWaveTrack(total, current)) {
    return null
  }

  const waves = total as number
  const now = current as number
  const closed = Math.min(Math.max(0, done), waves)
  const label = waveTrackLabel(now, waves, closed)

  const track =
    waves > MAX_SEGMENTS ? (
      <span
        aria-label={label}
        className={cn('flex min-w-0 items-center gap-1.5', className)}
        data-slot="wave-track"
        role="img"
      >
        <Progress
          aria-hidden
          className="w-16 bg-(--ui-stroke-secondary)"
          fillStyle={{ backgroundColor: 'var(--ui-green)' }}
          size="sm"
          value={closed / waves}
        />
        <span aria-hidden className="text-[0.65rem] tabular-nums text-(--ui-text-tertiary)">
          {`${now}/${waves}`}
        </span>
      </span>
    ) : (
      <span
        aria-label={label}
        className={cn('flex shrink-0 items-center gap-0.5', className)}
        data-slot="wave-track"
        role="img"
      >
        {Array.from({ length: waves }, (_, index) => {
          const state = index < closed ? 'closed' : index === now - 1 ? 'current' : 'future'

          return (
            <span
              className="h-1 w-2.5 shrink-0 rounded-[1px]"
              data-wave={state}
              key={index}
              style={{
                backgroundColor:
                  state === 'closed'
                    ? 'var(--ui-green)'
                    : state === 'current'
                      ? TONE_VAR[tone]
                      : 'var(--ui-stroke-secondary)'
              }}
            />
          )
        })}
      </span>
    )

  return <Tip label={label}>{track}</Tip>
}
