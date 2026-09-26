import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

import { StatusRow } from '@/components/chat/status-row'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { Codicon } from '@/components/ui/codicon'
import { revealTreePane } from '@/components/pane-shell/tree/store'
import { $relayJobsBySession } from '@/store/composer-status'
import { $activeSessionId } from '@/store/session'
import { $conductorBuildBySession } from '@/store/conductor-build'

interface ConductorPaneProps {
  sessionId?: string | null
}

const formatBuildTime = (value: number | string | null): string | null => {
  if (value === null || value === '' || value === 0 || value === '0') {
    return null
  }
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

export const $conductorPaneOpen = atom(false)

export function openConductorPane() {
  if ($conductorPaneOpen.get()) {
    revealTreePane('conductor')
  } else {
    $conductorPaneOpen.set(true)
  }
}

export function ConductorPane({ sessionId: sessionIdProp }: ConductorPaneProps = {}) {
  const activeSessionId = useStore($activeSessionId)
  const sessionId = sessionIdProp ?? activeSessionId
  const build = useStore($conductorBuildBySession)[sessionId ?? ''] ?? null
  const jobs = useStore($relayJobsBySession)[sessionId ?? ''] ?? []
  const waitSince = build ? formatBuildTime(build.wait_since) : null
  const armedAt = build ? formatBuildTime(build.armed_at) : null
  const leaseExpiresAt = build ? formatBuildTime(build.lease_expires_at) : null

  if (!build) {
    return <div className="p-3 text-sm text-(--ui-text-secondary)">No build armed in this workspace.</div>
  }

  const waveLabel = build.wave_current !== null
    && build.waves_total !== null
    && Number.isInteger(build.wave_current)
    && Number.isInteger(build.waves_total)
    && build.waves_total > 0
    ? `Wave ${build.wave_current}/${build.waves_total}`
    : null

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-auto p-3 text-sm" data-slot="conductor-pane">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt>Plan</dt><dd>{build.plan}</dd>
        <dt>Run</dt><dd>{build.run_id}</dd>
        {waveLabel && <><dt>Wave</dt><dd>{waveLabel}</dd></>}
        <dt>Done</dt><dd>Done {build.waves_done}</dd>
        <dt>State</dt><dd>{build.state === 'lease_expired' ? 'lease expired' : build.state}</dd>
        {build.waiting_on && <><dt>Waiting on</dt><dd>{build.waiting_on}</dd></>}
        {waitSince && <><dt>Wait since</dt><dd>{waitSince}</dd></>}
        {armedAt && <><dt>Armed</dt><dd>{armedAt}</dd></>}
        {leaseExpiresAt && <><dt>Lease expires</dt><dd>{leaseExpiresAt}</dd></>}
      </dl>
      <div>Cost: not metered</div>
      <div>Stage/unit: needs conductor T1</div>
      {build.lanes_build_matched > 0 && (
        <div className="flex items-center gap-1.5 text-xs" data-slot="conductor-build-lane-summary">
          <span className="rounded bg-(--ui-purple)/12 px-1 text-[0.58rem] text-(--ui-purple)">build</span>
          <span>{build.lanes_build_matched} build-matched lanes</span>
        </div>
      )}
      <div className="flex flex-col">
        {jobs.filter(job => job.status === 'running' || job.status === 'stale').map(job => (
          <StatusRow
            key={job.jobId}
            leading={<Codicon className="text-(--ui-text-tertiary)" name="server-process" size="0.75rem" />}
            trailing={job.durationSeconds === undefined ? undefined : <ActivityTimerText className="text-[0.65rem]" seconds={job.durationSeconds} />}
            trailingVisible={job.durationSeconds !== undefined}
          >
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5">
                <span className="truncate">{job.worker}</span>
                <span className="text-(--ui-text-tertiary)">{job.status}</span>
              </span>
              {(job.lane || job.role) && <span className="block text-[0.68rem] text-(--ui-text-tertiary)">{[job.lane, job.role].filter(Boolean).join(' · ')}</span>}
            </span>
          </StatusRow>
        ))}
      </div>
    </div>
  )
}
