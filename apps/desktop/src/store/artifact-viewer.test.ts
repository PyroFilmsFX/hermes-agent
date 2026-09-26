import { afterEach, describe, expect, it } from 'vitest'

import { $artifactViewerOpen, $artifactViewerTarget, closeArtifactViewer, openArtifactViewer } from './artifact-viewer'

afterEach(() => {
  closeArtifactViewer()
})

describe('artifact viewer target', () => {
  it('opens for a local job and retargets the single viewer', () => {
    openArtifactViewer({ sessionId: 'session-1', jobId: 'w_one' })
    expect($artifactViewerOpen.get()).toBe(true)
    expect($artifactViewerTarget.get()).toEqual({ sessionId: 'session-1', jobId: 'w_one' })
    openArtifactViewer({ sessionId: 'session-2', runId: 'run-2' })
    expect($artifactViewerTarget.get()).toEqual({ sessionId: 'session-2', runId: 'run-2' })
  })

  it('clears the target when closed', () => {
    openArtifactViewer({ sessionId: 'session-1', jobId: 'w_one' })
    closeArtifactViewer()
    expect($artifactViewerOpen.get()).toBe(false)
    expect($artifactViewerTarget.get()).toBeNull()
  })
})
