import { useState } from 'react'

import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { StatusRow } from '@/components/chat/status-row'
import { StatusSection } from '@/components/chat/status-section'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { useSessionSlice, useStoreSelector } from '@/lib/use-session-slice'
import { $sessionStates } from '@/store/session-states'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'

interface RelayJobsSectionProps {
  sessionId: string
}

const isRunning = (job: RelayJob) => job.status === 'running'
const isFailure = (job: RelayJob) => ['failed', 'error', 'cancelled', 'canceled', 'stale'].includes(job.status)

/** Conductor workers share the composer status shelf while retaining their own relay identity. */
export function RelayJobsSection({ sessionId }: RelayJobsSectionProps) {
  const jobs = useSessionSlice($relayJobsBySession, sessionId)
  const turnLive = useStoreSelector($sessionStates, states => {
    const state = states[sessionId]
    return Boolean(state && (state.busy || state.awaitingResponse || state.turnLive))
  })
  const visible = jobs.filter(job => isRunning(job) || turnLive)
  const live = visible.filter(isRunning)
  const [nowMs, setNowMs] = useState(Date.now)

  useViewedInterval(() => setNowMs(Date.now()), 1000, live.length > 0)

  if (!visible.length) {
    return null
  }

  return (
    <div className="composer-no-drag min-w-0" data-slot="composer-relay-jobs">
      <StatusSection
        collapsedIndicator={
          live.length ? (
            <GlyphSpinner ariaLabel="Running" className="text-(--ui-purple)" spinner="braille" />
          ) : (
            <Codicon aria-label="Relay jobs settled" className="text-(--ui-text-secondary)" name="check" size="0.75rem" />
          )
        }
        icon={<Codicon className="text-(--ui-purple)" name="server-process" size="0.8rem" />}
        label={`${visible.length} Relay job${visible.length === 1 ? '' : 's'}`}
      >
        <div className="max-h-[25vh] overflow-y-auto overscroll-y-auto">
          {visible.map(job => {
            const running = isRunning(job)
            const elapsed = running
              ? Math.max(0, Math.floor((nowMs - job.spawnedAt) / 1000))
              : Math.max(0, Math.floor(job.durationSeconds ?? 0))

            return (
              <StatusRow
                key={job.jobId}
                leading={
                  running ? (
                    <GlyphSpinner ariaLabel="Running" className="text-(--ui-purple)" spinner="braille" />
                  ) : (
                    <Codicon
                      aria-label={job.status}
                      className={isFailure(job) ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-secondary)'}
                      name={isFailure(job) ? 'circle-slash' : 'check'}
                      size="0.75rem"
                    />
                  )
                }
                trailing={
                  <span className="shrink-0 text-[0.65rem] text-(--ui-text-tertiary)">
                    <ActivityTimerText className="text-[0.65rem]" seconds={elapsed} />
                  </span>
                }
                trailingVisible
              >
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span
                      className="shrink-0 rounded bg-(--ui-purple)/12 px-1 text-[0.58rem] font-semibold uppercase tracking-wide text-(--ui-purple)"
                      data-slot="relay-badge"
                    >
                      Relay
                    </span>
                    <span className="truncate text-xs text-(--ui-text-primary)">{job.worker}</span>
                    {job.model && <span className="truncate text-[0.68rem] text-(--ui-text-secondary)">{job.model}</span>}
                    <span className="shrink-0 text-[0.68rem] text-(--ui-text-tertiary)">{job.status}</span>
                  </span>
                  {(job.lane || job.role) && (
                    <span className="block truncate text-[0.68rem] text-(--ui-text-tertiary)">
                      {[job.lane, job.role].filter(Boolean).join(' · ')}
                    </span>
                  )}
                </span>
              </StatusRow>
            )
          })}
        </div>
      </StatusSection>
    </div>
  )
}
