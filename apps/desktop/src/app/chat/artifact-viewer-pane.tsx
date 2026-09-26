import { useStore } from '@nanostores/react'
import { useLayoutEffect, useMemo, useRef, useState } from 'react'

import { WorkerRow } from '@/components/chat/worker-row'
import { Codicon } from '@/components/ui/codicon'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tip } from '@/components/ui/tooltip'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { isTerminalStatus } from '@/lib/conductor-seat'
import { $artifactViewerTarget, type ArtifactViewerTarget, type LocalArtifactSource } from '@/store/artifact-viewer'
import { $relayJobsBySession, type RelayJob } from '@/store/composer-status'
import { $activeSessionId } from '@/store/session'

import { parseCodexTranscript } from './artifact-viewer/codex-transcript'
import { FinalReport, ReadableTranscript, resultLine } from './artifact-viewer/readable-transcript'
import { useArtifactSource } from './artifact-viewer/use-artifact-source'

interface ArtifactViewerPaneProps {
  target?: ArtifactViewerTarget | null
}

const EMPTY_COPY = 'This worker has not written any output yet.'
const EMPTY_JOBS: RelayJob[] = []

const sizeLabel = (bytes: number) =>
  bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`

const sensitiveSentence = (source: LocalArtifactSource) =>
  `This log is marked sensitive, so only its size is shown here: ${sizeLabel(source.bytes)}${
    source.lines === null ? '' : `, ${source.lines.toLocaleString('en-US')} lines`
  }.`

const errorCopy = (error: string, failures: number) =>
  error === 'read'
    ? failures >= 3
      ? "Couldn't read this log. Live updates paused; reopen the pane to retry."
      : "Couldn't read this log. Retrying every 2 seconds."
    : error === 'earlier'
      ? "Couldn't load earlier output."
      : "Couldn't download this log."

/** The header's worker: the live composer row when it is still listed, else the opening row's hints. */
function headerJob(target: ArtifactViewerTarget, live: RelayJob | undefined, source: LocalArtifactSource | null): RelayJob {
  const hints = target.hints ?? {}
  const status = live?.status ?? source?.status ?? hints.status ?? ''

  if (live) {
    return { ...live, status }
  }

  return {
    buildMatch: false,
    durationSeconds: hints.durationSeconds,
    effort: hints.effort ?? '',
    exitCode: hints.exitCode,
    jobId: target.jobId ?? '',
    label: hints.label ?? '',
    lane: hints.lane ?? '',
    model: hints.model ?? '',
    place: hints.place ?? '',
    purpose: hints.purpose ?? '',
    role: '',
    spawnedAt: hints.spawnedAt ?? Number.NaN,
    status,
    worker: hints.worker ?? source?.worker ?? ''
  }
}

export function ArtifactViewerPane({ target: targetProp }: ArtifactViewerPaneProps) {
  const activeSessionId = useStore($activeSessionId)
  const storedTarget = useStore($artifactViewerTarget)
  const target = targetProp ?? storedTarget ?? (activeSessionId ? { sessionId: activeSessionId } : null)

  if (!target) {
    return <div className="p-3 text-xs text-(--ui-text-secondary)">{EMPTY_COPY}</div>
  }

  return <ArtifactViewer target={target} />
}

function ArtifactViewer({ target }: { target: ArtifactViewerTarget }) {
  const { snapshot, source, window, error, failures, loadEarlier, loadingEarlier, download, variant, selectVariant } =
    useArtifactSource(target)
  const sessionJobs = useStore($relayJobsBySession)[target.sessionId] ?? EMPTY_JOBS
  const live = target.jobId ? sessionJobs.find(job => job.jobId === target.jobId) : undefined
  const job = headerJob(target, live, source)
  const running = job.status === 'running'
  const [view, setView] = useState<'raw' | 'readable'>('readable')
  const [nowMs, setNowMs] = useState(Date.now)
  const parsed = useMemo(() => (window ? parseCodexTranscript(window.text, window.bof) : null), [window])
  const scrollRef = useRef<HTMLDivElement>(null)
  const stuckRef = useRef(true)

  useViewedInterval(() => setNowMs(Date.now()), 1000, running)

  // Follow the tail while the worker runs, unless the reader scrolled up.
  useLayoutEffect(() => {
    const node = scrollRef.current

    if (node && stuckRef.current) {
      node.scrollTop = node.scrollHeight
    }
  }, [window?.text, view])

  const variants = new Set((snapshot?.local ?? []).map(item => item.variant))
  const readable = Boolean(parsed?.codex) && view === 'readable' && variant === 'log'
  const report = readable && isTerminalStatus(job.status) ? parsed?.finalReport ?? null : null

  const download_ =
    source?.viewable === 'text' ? (
      <Tip label={`${source.job_id}${source.variant === 'log' ? '.log' : '.agy.log'} · ${sizeLabel(source.bytes)}`}>
        <button
          aria-label="Download log"
          className="grid size-5 place-items-center rounded text-(--ui-text-tertiary) hover:bg-(--ui-row-hover-background) hover:text-(--ui-text-secondary)"
          onClick={event => {
            event.stopPropagation()
            void download()
          }}
          type="button"
        >
          <Codicon name="desktop-download" size="0.8rem" />
        </button>
      </Tip>
    ) : undefined

  const controls = (
    <>
      {parsed?.codex && variant === 'log' && (
        <SegmentedControl
          onChange={setView}
          options={[
            { id: 'readable', label: 'Readable' },
            { id: 'raw', label: 'Raw' }
          ]}
          value={view}
        />
      )}
      {variants.has('log') && variants.has('agy_log') && (
        <SegmentedControl
          onChange={selectVariant}
          options={[
            { id: 'log', label: 'Transcript' },
            { id: 'agy_log', label: 'Debug log' }
          ]}
          value={variant}
        />
      )}
    </>
  )

  const hasText = Boolean(window?.text.trim())

  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-hidden px-2 py-1.5 text-xs" data-slot="artifact-viewer-pane">
      <header className="shrink-0">
        <WorkerRow job={job} metaExtra={controls} nowMs={nowMs} trailingExtra={download_} />
      </header>
      {source?.viewable === 'metadata' ? (
        <Tip label={source.sha256 ? `SHA-256 ${source.sha256}` : undefined}>
          <p className="px-1.5 text-(--ui-text-secondary)" data-slot="artifact-metadata">
            {sensitiveSentence(source)}
          </p>
        </Tip>
      ) : window && hasText ? (
        <>
          {report && <FinalReport result={resultLine(job.status, job.durationSeconds, job.exitCode)} text={report} />}
          <div
            className="min-h-0 flex-1 overflow-auto px-1.5"
            onScroll={event => {
              const node = event.currentTarget
              stuckRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24
            }}
            ref={scrollRef}
          >
            {!window.bof && (
              <button
                className="mb-1.5 text-[0.68rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)"
                disabled={loadingEarlier}
                onClick={() => void loadEarlier()}
                type="button"
              >
                Show earlier output
              </button>
            )}
            {readable && parsed ? (
              <ReadableTranscript entries={parsed.entries} live={running} />
            ) : (
              <pre
                className={
                  parsed?.codex || variant === 'agy_log'
                    ? 'whitespace-pre-wrap break-words font-mono text-[0.68rem] text-(--ui-text-secondary)'
                    : 'whitespace-pre-wrap break-words font-sans text-xs text-(--ui-text-primary)'
                }
                data-slot="artifact-text-window"
              >
                {window.text}
              </pre>
            )}
          </div>
        </>
      ) : snapshot && (window || !source) ? (
        <p className="px-1.5 text-(--ui-text-tertiary)">{EMPTY_COPY}</p>
      ) : null}
      {error && (
        <div className="px-1.5 text-[0.68rem] text-(--ui-text-tertiary)" role="alert">
          {errorCopy(error, failures)}
        </div>
      )}
    </section>
  )
}
