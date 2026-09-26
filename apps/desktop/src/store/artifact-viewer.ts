import { atom } from 'nanostores'

export interface ArtifactViewerTarget {
  sessionId: string
  jobId?: string
  runId?: string
  artifactId?: string
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

export function artifactViewerKey(target: ArtifactViewerTarget): string {
  return JSON.stringify(target)
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
