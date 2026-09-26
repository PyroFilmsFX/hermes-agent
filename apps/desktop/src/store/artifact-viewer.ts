import { atom } from 'nanostores'

/** Display-only hints from the row that opened the pane, so the header paints before the
 *  first list call. Never used to pick what is read. */
export interface ArtifactViewerHints {
  durationSeconds?: number
  effort?: string
  exitCode?: number
  label?: string
  lane?: string
  model?: string
  place?: string
  purpose?: string
  spawnedAt?: number
  status?: string
  worker?: string
}

export interface ArtifactViewerTarget {
  sessionId: string
  jobId?: string
  runId?: string
  artifactId?: string
  hints?: ArtifactViewerHints
}

export interface LocalArtifactSource {
  job_id: string
  variant: 'log' | 'agy_log'
  bytes: number
  lines: number | null
  mtime: string
  data_class: 'A' | 'B' | 'C' | 'unknown'
  viewable: 'text' | 'metadata'
  sha256: string | null
  status: string
  worker: string
}

export interface ArtifactListSnapshot {
  durable: { state: 'unavailable'; reason: 'not_configured' }
  local: LocalArtifactSource[] | null
  local_reason: 'none' | 'no_record' | 'path_mismatch' | 'unreadable' | null
}

export const $artifactViewerOpen = atom(false)
export const $artifactViewerTarget = atom<ArtifactViewerTarget | null>(null)
export const $artifactListByTarget = atom<Record<string, ArtifactListSnapshot>>({})

/** Identity of what is read: display hints are excluded, so a status change on the opening
 *  row never resets the tail. */
export function artifactViewerKey(target: ArtifactViewerTarget): string {
  const { artifactId, jobId, runId, sessionId } = target

  return JSON.stringify({ sessionId, jobId, runId, artifactId })
}

export function cacheArtifactList(target: ArtifactViewerTarget, snapshot: ArtifactListSnapshot) {
  $artifactListByTarget.set({ ...$artifactListByTarget.get(), [artifactViewerKey(target)]: snapshot })
}

export function openArtifactViewer(target: ArtifactViewerTarget) {
  $artifactViewerTarget.set(target)
  $artifactViewerOpen.set(true)
}

export function closeArtifactViewer() {
  $artifactViewerOpen.set(false)
  $artifactViewerTarget.set(null)
}
