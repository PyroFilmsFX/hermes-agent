import { useStore } from '@nanostores/react'

import { $activeSessionId } from '@/store/session'
import { $artifactViewerTarget, type ArtifactViewerTarget } from '@/store/artifact-viewer'

import { useArtifactSource } from './artifact-viewer/use-artifact-source'

interface ArtifactViewerPaneProps {
  target?: ArtifactViewerTarget | null
}

const sizeLabel = (bytes: number) => bytes < 1024 * 1024
  ? `${Math.max(1, Math.round(bytes / 1024))} KB`
  : `${(bytes / (1024 * 1024)).toFixed(1)} MB`

export function ArtifactViewerPane({ target: targetProp }: ArtifactViewerPaneProps) {
  const activeSessionId = useStore($activeSessionId)
  const storedTarget = useStore($artifactViewerTarget)
  const target = targetProp ?? storedTarget ?? (activeSessionId ? { sessionId: activeSessionId } : null)
  if (!target) return <div className="p-3 text-sm text-(--ui-text-secondary)">No transcript for this lane yet.</div>
  return <ArtifactViewer target={target} />
}

function ArtifactViewer({ target }: { target: ArtifactViewerTarget }) {
  const { snapshot, source, window, error, failures, loadEarlier, loadingEarlier, download } = useArtifactSource(target)
  const badge = snapshot?.durable.state === 'unavailable' ? 'local' : 'local'
  const header = source ? `${source.worker} · ${source.job_id} · ${source.status}` : `Worker · ${target.jobId ?? target.runId ?? 'artifacts'}`
  const sources = snapshot?.local ?? []

  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-hidden p-3 text-sm" data-slot="artifact-viewer-pane">
      <header className="flex items-center justify-between gap-3">
        <h2 className="truncate font-medium">{header}</h2>
        <span className="shrink-0 rounded bg-(--ui-row-hover-background) px-1.5 py-0.5 text-xs text-(--ui-text-secondary)" data-slot="artifact-source-badge">{badge}</span>
      </header>
      <div className="text-xs text-(--ui-text-secondary)" role="status">Not uploaded to cntrl yet: showing local log</div>
      {sources.length > 0 && (
        <nav aria-label="Artifact sources" className="flex flex-wrap gap-1 border-b border-(--ui-border) pb-2">
          {sources.map(item => (
            <span className="rounded bg-(--ui-row-hover-background) px-2 py-1 text-xs" key={item.variant}>
              {item.variant === 'log' ? 'log' : 'agy log'} · {sizeLabel(item.bytes)} · class {item.data_class}
            </span>
          ))}
        </nav>
      )}
      {source?.viewable === 'metadata' ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs" data-slot="artifact-metadata">
          <dt>Bytes</dt><dd>{source.bytes}</dd>
          <dt>Lines</dt><dd>{source.lines ?? 'unknown'}</dd>
          <dt>Class</dt><dd>{source.data_class}</dd>
          <dt>SHA-256</dt><dd className="break-all font-mono">{source.sha256 ?? 'Unavailable above 64 MiB'}</dd>
        </dl>
      ) : window ? (
        <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden">
          {!window.bof && <button className="self-start text-xs text-(--ui-text-secondary)" disabled={loadingEarlier} onClick={() => void loadEarlier()} type="button">Load earlier</button>}
          <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words rounded bg-(--ui-input-background) p-2 font-mono text-xs" data-slot="artifact-text-window">{window.text || ' '}</pre>
          {source && <button className="self-start text-xs text-(--ui-text-secondary)" onClick={() => void download()} type="button">Download</button>}
        </div>
      ) : (
        <div className="text-sm text-(--ui-text-secondary)">{snapshot ? 'No transcript for this lane yet.' : 'Loading local log…'}</div>
      )}
      {error && <div className="text-xs text-(--ui-text-secondary)" role="alert">{error}{failures >= 3 ? ' Live tail paused after repeated read failures.' : ''}</div>}
    </section>
  )
}
