import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'

import { AvatarChip } from '@/components/ui/avatar-chip'
import { OverflowTip, Tip } from '@/components/ui/tooltip'
import {
  exitNote,
  kindLabel,
  modelLabel,
  seatLabel,
  seatMonogram,
  statusLabel
} from '@/lib/conductor-seat'
import { cn } from '@/lib/utils'
import type { RelayJob } from '@/store/composer-status'

import { ActivityTimerText } from './activity-timer-text'
import { StatusChip } from './status-chip'
import { StatusRow } from './status-row'

/** Row name: the caller's label, then the lane directory, then the kind. Never an id. */
export const workerName = (job: RelayJob) =>
  job.label || job.place || kindLabel(job.lane) || seatLabel(job.worker, job.model)

export const workerElapsedSeconds = (job: RelayJob, nowMs: number): number | undefined =>
  job.status === 'running'
    ? Number.isFinite(job.spawnedAt)
      ? Math.max(0, Math.floor((nowMs - job.spawnedAt) / 1000))
      : undefined
    : job.durationSeconds === undefined
      ? undefined
      : Math.max(0, Math.floor(job.durationSeconds))

export function SeatGlyph({ job }: { job: RelayJob }) {
  const seat = seatLabel(job.worker, job.model)
  const model = modelLabel(job.model, job.effort)
  const tip = [seat, model, job.effort ? `${job.effort} effort` : ''].filter(Boolean).join(', ')

  return (
    <Tip label={tip}>
      <AvatarChip
        brand={null}
        className="size-4 rounded-[4px] text-[0.5rem] font-semibold leading-none"
        data-slot="seat-glyph"
        name={seat}
      >
        {seatMonogram(job.worker, job.model)}
      </AvatarChip>
    </Tip>
  )
}

/** Seat · model, kind, place, then the exit note: the row's quiet second line. */
export function WorkerMeta({ className, job }: { className?: string; job: RelayJob }) {
  const seat = seatLabel(job.worker, job.model)
  const model = modelLabel(job.model, job.effort)
  const note = exitNote(job.status, job.exitCode)

  return (
    <span className={cn('min-w-0 items-center gap-3 text-[0.68rem] text-(--ui-text-tertiary)', className)}>
      <span className="shrink-0 whitespace-nowrap">{model ? `${seat} · ${model}` : seat}</span>
      {job.lane && <span className="shrink-0 whitespace-nowrap">{kindLabel(job.lane)}</span>}
      {job.place && <span className="min-w-0 truncate">{job.place}</span>}
      {note && (
        <span
          className={cn('shrink-0 whitespace-nowrap', job.status === 'timeout' ? undefined : 'text-(--ui-red)')}
          data-slot="worker-row-exit"
        >
          {note}
        </span>
      )}
    </span>
  )
}

interface WorkerRowProps {
  className?: string
  job: RelayJob
  nowMs: number
  /** Controls at the end of the narrow second line (the output pane's view toggles). */
  metaExtra?: ReactNode
  onActivate?: (event: KeyboardEvent | MouseEvent) => void
  /** Extra trailing controls (the output pane's download button). */
  trailingExtra?: ReactNode
}

/**
 * One worker, two densities. Narrow (composer stack, narrow panes): name + purpose, then a
 * quiet second line. Wide (`@3xl` inside a `@container`): everything on one line.
 */
export function WorkerRow({ className, job, metaExtra, nowMs, onActivate, trailingExtra }: WorkerRowProps) {
  const name = workerName(job)
  const seat = seatLabel(job.worker, job.model)
  const elapsed = workerElapsedSeconds(job, nowMs)

  return (
    <StatusRow
      aria-label={onActivate ? `Open output for ${name}, ${seat}, ${statusLabel(job.status, job.lane)}` : undefined}
      className={className}
      leading={<SeatGlyph job={job} />}
      onActivate={onActivate}
      trailing={
        <>
          {elapsed !== undefined && (
            <span className="mr-1.5 shrink-0" data-slot="worker-row-elapsed">
              <ActivityTimerText className="text-[0.65rem] tabular-nums text-(--ui-text-tertiary)" seconds={elapsed} />
            </span>
          )}
          <StatusChip lane={job.lane} status={job.status} />
          {trailingExtra}
        </>
      }
      trailingVisible
    >
      <span className="flex min-w-0 flex-1 flex-col" data-slot="worker-row">
        <span className="flex min-w-0 items-center gap-2">
          <span className="max-w-[12ch] shrink-0 truncate text-xs font-medium text-(--ui-text-primary)">{name}</span>
          {job.purpose && (
            <OverflowTip label={job.purpose}>
              <span className="min-w-0 flex-1 truncate text-xs text-(--ui-text-secondary)">{job.purpose}</span>
            </OverflowTip>
          )}
          <WorkerMeta className="hidden @3xl:flex @3xl:shrink-0" job={job} />
        </span>
        <span className="flex min-w-0 items-center gap-3 @3xl:hidden">
          <WorkerMeta className="flex" job={job} />
          {metaExtra && <span className="ml-auto flex shrink-0 items-center gap-1.5">{metaExtra}</span>}
        </span>
      </span>
    </StatusRow>
  )
}
