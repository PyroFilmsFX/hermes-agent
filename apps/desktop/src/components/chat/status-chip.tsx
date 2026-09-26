import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Codicon } from '@/components/ui/codicon'
import { StatusPulse } from '@/components/ui/status-pulse'
import { Tip } from '@/components/ui/tooltip'
import { statusLabel, statusTone, type StatusTone } from '@/lib/conductor-seat'
import { cn } from '@/lib/utils'

/** The one place a state hue is chosen. Colour means state and nothing else. */
export const TONE_VAR: Record<StatusTone, string> = {
  attention: 'var(--ui-yellow)',
  /** Quiet on purpose: an idle build is not a problem, so it takes no state hue. */
  idle: 'var(--ui-text-tertiary)',
  live: 'var(--ui-purple)',
  ok: 'var(--ui-green)',
  stop: 'var(--ui-red)',
  wait: 'var(--ui-blue)'
}

const TONE_GLYPH: Record<Exclude<StatusTone, 'live'>, string> = {
  attention: 'warning',
  idle: 'circle-outline',
  ok: 'check',
  stop: 'error',
  wait: 'watch'
}

export function ToneGlyph({ className, tone }: { className?: string; tone: StatusTone }) {
  if (tone === 'live') {
    return (
      <StatusPulse
        aria-hidden
        className={cn('inline-block size-1.5 shrink-0 rounded-full', className)}
        data-slot="status-chip-glyph"
        kind="opacity"
        style={{ backgroundColor: TONE_VAR.live }}
      />
    )
  }

  return (
    <Codicon
      className={cn('shrink-0', className)}
      data-slot="status-chip-glyph"
      name={TONE_GLYPH[tone]}
      size="0.7rem"
      style={{ color: TONE_VAR[tone] }}
    />
  )
}

interface StatusChipProps {
  className?: string
  /** Replaces the status word (the strip's `Build idle since 09:58`). The tone still follows `status`. */
  label?: string
  /** Council rows answer rather than finish. */
  lane?: string
  /** Wire status (job) or build state; unknown values render as attention with the wire word. */
  status: string
  tip?: ReactNode
}

/**
 * State chip: the tone lives in a 12% fill and a shaped leading glyph. The label stays
 * `--ui-text-secondary` (tone text fails 4.5:1 in one theme or the other), except `stop`,
 * whose red passes in both.
 */
export function StatusChip({ className, label: labelOverride, lane, status, tip }: StatusChipProps) {
  const tone = statusTone(status)
  const label = labelOverride || statusLabel(status, lane)

  const chip = (
    <Badge
      className={cn('shrink-0 whitespace-nowrap rounded-[3px] text-(--ui-text-secondary)', className)}
      data-slot="status-chip"
      data-tone={tone}
      style={{ backgroundColor: `color-mix(in srgb, ${TONE_VAR[tone]} 12%, transparent)` }}
    >
      <ToneGlyph tone={tone} />
      <span
        className={tone === 'stop' ? 'text-(--ui-red)' : 'text-(--ui-text-secondary)'}
        data-slot="status-chip-label"
      >
        {label}
      </span>
    </Badge>
  )

  return tip ? <Tip label={tip}>{chip}</Tip> : chip
}
