import { useState } from 'react'

import { StatusSection } from '@/components/chat/status-section'
import { WorkerRow } from '@/components/chat/worker-row'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { statusTone } from '@/lib/conductor-seat'
import { useSessionSlice } from '@/lib/use-session-slice'
import { openArtifactViewer } from '@/store/artifact-viewer'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'

interface RelayJobsSectionProps {
  sessionId: string
}

const PREVIEW_LIMIT = 3

const isRunning = (job: RelayJob) => job.status === 'running'
const needsAttention = (job: RelayJob) => !isRunning(job) && ['attention', 'stop'].includes(statusTone(job.status))
const isFailed = (job: RelayJob) => ['failed', 'error', 'timeout', 'cancelled', 'canceled'].includes(job.status)

const groupRank = (job: RelayJob) => (isRunning(job) ? 0 : needsAttention(job) ? 1 : 2)

/** Running, then needs attention, then done; newest first within each group. */
export const orderWorkers = (jobs: readonly RelayJob[]) =>
  [...jobs].sort((a, b) => groupRank(a) - groupRank(b) || b.spawnedAt - a.spawnedAt)

/** `2 running, 1 failed, 1 not responding`; zero counts are omitted. */
export function workerSummary(jobs: readonly RelayJob[]): string {
  const running = jobs.filter(isRunning).length
  const failed = jobs.filter(isFailed).length
  const stale = jobs.filter(job => job.status === 'stale').length

  return [
    running ? `${running} running` : '',
    failed ? `${failed} failed` : '',
    stale ? `${stale} not responding` : ''
  ]
    .filter(Boolean)
    .join(', ')
}

/** The row passes display hints so the output pane's header paints before its first read. */
export function openWorkerOutput(sessionId: string, job: RelayJob) {
  openArtifactViewer({
    hints: {
      durationSeconds: job.durationSeconds,
      effort: job.effort,
      exitCode: job.exitCode,
      label: job.label,
      lane: job.lane,
      model: job.model,
      place: job.place,
      purpose: job.purpose,
      spawnedAt: job.spawnedAt,
      status: job.status,
      worker: job.worker
    },
    jobId: job.jobId,
    sessionId
  })
}

/**
 * Conductor workers in the composer stack. Rows live exactly as long as the gateway lists
 * them (10 min after finishing; an hour for a worker that stopped responding): the turn
 * ending does not hide them. Collapsed, the running and failing workers stay in view.
 */
export function RelayJobsSection({ sessionId }: RelayJobsSectionProps) {
  const jobs = useSessionSlice($relayJobsBySession, sessionId)
  const ordered = orderWorkers(jobs)
  const live = ordered.filter(isRunning)
  const [nowMs, setNowMs] = useState(Date.now)

  useViewedInterval(() => setNowMs(Date.now()), 1000, live.length > 0)

  if (!ordered.length) {
    return null
  }

  const salient = ordered.filter(job => isRunning(job) || needsAttention(job))
  const summary = workerSummary(ordered)

  const row = (job: RelayJob) => (
    <WorkerRow job={job} key={job.jobId} nowMs={nowMs} onActivate={() => openWorkerOutput(sessionId, job)} />
  )

  return (
    <div className="composer-no-drag min-w-0" data-slot="composer-relay-jobs">
      <StatusSection
        accessory={
          summary ? <span className="pr-1 text-[0.68rem] text-(--ui-text-tertiary)">{summary}</span> : undefined
        }
        collapsedIndicator={
          live.length ? (
            <GlyphSpinner ariaLabel="Running" className="text-(--ui-purple)" spinner="braille" />
          ) : undefined
        }
        icon={<Codicon className="text-(--ui-text-tertiary)" name="server-process" size="0.8rem" />}
        label="Workers"
        preview={
          salient.length ? (
            <div data-slot="composer-relay-preview">
              {salient.slice(0, PREVIEW_LIMIT).map(row)}
              {salient.length > PREVIEW_LIMIT && (
                <div className="px-1.5 py-0.5 text-[0.68rem] text-(--ui-text-tertiary)">
                  {`+${salient.length - PREVIEW_LIMIT} more`}
                </div>
              )}
            </div>
          ) : undefined
        }
      >
        <div className="max-h-[25vh] overflow-y-auto overscroll-y-auto">{ordered.map(row)}</div>
      </StatusSection>
    </div>
  )
}
