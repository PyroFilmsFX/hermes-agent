import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { type ReactNode, useState } from 'react'

import { DisclosureRow } from '@/components/chat/disclosure-row'
import { StatusChip } from '@/components/chat/status-chip'
import { WaveTrack, hasWaveTrack } from '@/components/chat/wave-track'
import { WorkerRow } from '@/components/chat/worker-row'
import { revealTreePane } from '@/components/pane-shell/tree/store'
import { Codicon } from '@/components/ui/codicon'
import { CopyButton } from '@/components/ui/copy-button'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { OverflowTip, Tip } from '@/components/ui/tooltip'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { statusTone } from '@/lib/conductor-seat'
import { formatStamp, idleSentence, leaseExpiredSentence, markerStaleSentence, parseBuildTime, relativeAge } from '@/lib/conductor-time'
import { planTitle } from '@/lib/plan-title'
import { cn } from '@/lib/utils'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'
import { $conductorBuildBySession, type ConductorBuild } from '@/store/conductor-build'
import { $activeSessionId } from '@/store/session'

import { buildChipTip } from './composer/status-stack/conductor-build-strip'
import { openWorkerOutput, orderWorkers, workerSummary } from './composer/status-stack/relay-jobs-section'
import { useBuildWorkers } from './use-build-workers'

interface ConductorPaneProps {
  sessionId?: string | null
}

const EMPTY_JOBS: RelayJob[] = []

export const $conductorPaneOpen = atom(false)

export function openConductorPane() {
  if ($conductorPaneOpen.get()) {
    revealTreePane('conductor')
  } else {
    $conductorPaneOpen.set(true)
  }
}

const groupSummary = (jobs: readonly RelayJob[]) => {
  const counts = workerSummary(jobs)

  return `${jobs.length} worker${jobs.length === 1 ? '' : 's'}${counts ? `, ${counts}` : ''}`
}

const stateLine = (build: ConductorBuild): string | null =>
  build.state === 'waiting' && build.waiting_on
    ? `Waiting on ${build.waiting_on}`
    : build.state === 'blocked'
      ? 'Needs your input in the conductor session.'
      : build.state === 'lease_expired'
        ? leaseExpiredSentence(build.lease_expires_at)
        : build.state === 'idle'
          ? idleSentence(build.idle_since)
          : null

/** `484496…c677d27`: enough to recognise, never the whole hash on first read. */
const shortId = (id: string) => (id.length > 16 ? `${id.slice(0, 6)}…${id.slice(-7)}` : id)

function WorkerGroup({
  children,
  first,
  label,
  onToggle,
  open,
  slot,
  summary
}: {
  children: ReactNode
  first: boolean
  label: string
  onToggle: () => void
  open: boolean
  slot: string
  summary?: string
}) {
  return (
    <section className={cn(!first && 'border-t border-(--ui-stroke-tertiary)')} data-slot={slot}>
      <button
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-1.5 py-1 text-left text-[0.68rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)"
        onClick={onToggle}
        type="button"
      >
        <DisclosureCaret open={open} size="0.7rem" />
        <span className="shrink-0">{label}</span>
        {summary && <span className="ml-auto min-w-0 truncate">{summary}</span>}
      </button>
      {open && children}
    </section>
  )
}

function DetailRow({ children, label }: { children: ReactNode; label: string }) {
  return (
    <>
      <dt className="text-(--ui-text-tertiary)">{label}</dt>
      <dd className="flex min-w-0 items-center gap-1 text-(--ui-text-secondary)">{children}</dd>
    </>
  )
}

function BuildDetails({ build }: { build: ConductorBuild }) {
  const [open, setOpen] = useState(false)
  const armed = parseBuildTime(build.armed_at)
  const lease = parseBuildTime(build.lease_expires_at)

  return (
    <div className="border-t border-(--ui-stroke-tertiary) px-1.5 py-1 text-[0.68rem]">
      <DisclosureRow onToggle={() => setOpen(value => !value)} open={open}>
        <span className="text-[0.68rem]">Build details</span>
      </DisclosureRow>
      {open && (
        <dl
          className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1"
          data-slot="conductor-build-details"
        >
          {build.run_id && (
            <DetailRow label="Run">
              <span className="truncate font-mono">{shortId(build.run_id)}</span>
              <CopyButton appearance="icon" label="Copy run id" text={build.run_id} />
            </DetailRow>
          )}
          {build.session_id && (
            <DetailRow label="Session">
              <span className="truncate font-mono">{build.session_id}</span>
              <CopyButton appearance="icon" label="Copy session id" text={build.session_id} />
            </DetailRow>
          )}
          {armed && (
            <DetailRow label="Armed">
              <Tip label={relativeAge(armed)}>
                <span>{formatStamp(armed)}</span>
              </Tip>
            </DetailRow>
          )}
          {lease && (
            <DetailRow label="Lease">
              <Tip label={relativeAge(lease)}>
                <span>{`${build.state === 'lease_expired' ? 'Expired' : 'Renews by'} ${formatStamp(lease)}`}</span>
              </Tip>
            </DetailRow>
          )}
          <DetailRow label="Plan file">
            <span className="truncate">{build.plan}</span>
          </DetailRow>
        </dl>
      )}
    </div>
  )
}

function PaneHeader({ build }: { build: ConductorBuild }) {
  const line = stateLine(build)
  const markerHint = build.state === 'idle' ? null : markerStaleSentence(build.marker_stale_since)
  const showWave = hasWaveTrack(build.waves_total, build.wave_current)

  return (
    <header className="flex shrink-0 flex-col gap-0.5 border-b border-(--ui-stroke-tertiary) px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <Codicon className="text-(--ui-text-tertiary)" name="layers" size="0.85rem" />
        <Tip label={build.plan}>
          <span className="min-w-0 truncate text-sm font-medium text-(--ui-text-primary)">{planTitle(build.plan)}</span>
        </Tip>
        <StatusChip status={build.state} tip={buildChipTip(build)} />
        {showWave && (
          <span className="ml-auto flex shrink-0 items-center gap-2">
            <WaveTrack
              current={build.wave_current}
              done={build.waves_done}
              tone={statusTone(build.state)}
              total={build.waves_total}
            />
            <span className="whitespace-nowrap text-(--ui-text-secondary)">
              {`Wave ${build.wave_current} of ${build.waves_total}`}
            </span>
          </span>
        )}
      </div>
      {line && (
        <OverflowTip label={line}>
          <span className="truncate pl-[1.35rem] text-(--ui-text-tertiary)">{line}</span>
        </OverflowTip>
      )}
      {markerHint && (
        <span className="truncate pl-[1.35rem] text-(--ui-text-tertiary)" data-slot="conductor-marker-hint">
          {markerHint}
        </span>
      )}
    </header>
  )
}

export function ConductorPane({ sessionId: sessionIdProp }: ConductorPaneProps = {}) {
  const activeSessionId = useStore($activeSessionId)
  const sessionId = sessionIdProp ?? activeSessionId
  const build = useStore($conductorBuildBySession)[sessionId ?? ''] ?? null
  const recentJobs = useStore($relayJobsBySession)[sessionId ?? ''] ?? EMPTY_JOBS
  const buildJobs = useBuildWorkers(sessionId ?? null, build?.run_id || null)
  const [otherOpen, setOtherOpen] = useState<boolean | null>(null)
  const [buildOpen, setBuildOpen] = useState(true)
  const [nowMs, setNowMs] = useState(Date.now)

  const thisBuild = orderWorkers(buildJobs ?? EMPTY_JOBS)
  const other = orderWorkers(recentJobs.filter(job => !job.buildMatch))
  const anyRunning = thisBuild.some(job => job.status === 'running') || other.some(job => job.status === 'running')

  useViewedInterval(() => setNowMs(Date.now()), 1000, anyRunning)

  const row = (job: RelayJob) => (
    <WorkerRow
      job={job}
      key={job.jobId}
      nowMs={nowMs}
      onActivate={() => sessionId && openWorkerOutput(sessionId, job)}
    />
  )

  const otherExpanded = otherOpen ?? (!build || (buildJobs !== null && thisBuild.length === 0))

  const otherGroup = other.length > 0 && (
    <WorkerGroup
      first={!build}
      label="Other recent work"
      onToggle={() => setOtherOpen(!otherExpanded)}
      open={otherExpanded}
      slot="conductor-group-other"
      summary={groupSummary(other)}
    >
      {other.map(row)}
    </WorkerGroup>
  )

  if (!build) {
    return (
      <div className="@container flex h-full min-h-0 flex-col text-xs" data-slot="conductor-pane">
        <div className="min-h-0 flex-1 overflow-auto px-1.5 py-2">
          <p className="px-1.5 pb-2 text-(--ui-text-secondary)">
            No build is armed in this workspace. Start one with /tb-build in a conductor session.
          </p>
          {otherGroup}
        </div>
      </div>
    )
  }

  return (
    <div className="@container flex h-full min-h-0 flex-col text-xs" data-slot="conductor-pane">
      <PaneHeader build={build} />
      <div className="min-h-0 flex-1 overflow-auto px-1.5 py-1">
        <WorkerGroup
          first
          label="This build"
          onToggle={() => setBuildOpen(open => !open)}
          open={buildOpen}
          slot="conductor-group-build"
          summary={thisBuild.length ? groupSummary(thisBuild) : undefined}
        >
          {thisBuild.length
            ? thisBuild.map(row)
            : buildJobs !== null && (
                <p className="px-1.5 py-1 text-(--ui-text-tertiary)">No workers have run for this build yet.</p>
              )}
        </WorkerGroup>
        {otherGroup}
        <BuildDetails build={build} />
      </div>
    </div>
  )
}
