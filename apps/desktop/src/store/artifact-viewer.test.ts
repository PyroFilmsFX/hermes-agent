import { afterEach, describe, expect, it } from 'vitest'

import {
  $artifactViewerOpen,
  $artifactViewerTarget,
  artifactViewerKey,
  closeArtifactViewer,
  openArtifactViewer
} from './artifact-viewer'

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

  it('keys reads on what is read, not on the display hints', () => {
    const base = { jobId: 'w_one', sessionId: 'session-1' }

    expect(artifactViewerKey({ ...base, hints: { status: 'running' } })).toBe(
      artifactViewerKey({ ...base, hints: { label: 'fix-g9', status: 'succeeded' } })
    )
    expect(artifactViewerKey(base)).not.toBe(artifactViewerKey({ ...base, jobId: 'w_two' }))
  })
})
