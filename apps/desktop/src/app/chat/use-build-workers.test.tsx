import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RelayJob } from '@/store/composer-status'

const request = vi.fn()

vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<typeof import('@/store/session-states')>()),
  requestForOwnedSession: (...args: unknown[]) => request(...args)
}))

const { useBuildWorkers } = await import('./use-build-workers')

const RUN_A = 'a'.repeat(32)
const RUN_B = 'b'.repeat(32)

const wireRow = (overrides: Record<string, unknown>) => ({
  build_match: true, duration_sec: 60, effort: '', exit_code: null, heartbeat_at: '',
  job_id: 'w_20260926T202751Z_0e14', label: 'impl', lane: 'impl', model: 'gpt-6-sol', model_resolved: '',
  place: 'lane-a', purpose: '', role: '', spawned_at: '2026-09-26T20:27:51Z', status: 'running', worker: 'codex',
  ...overrides
})

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

afterEach(() => {
  cleanup()
  request.mockReset()
})

describe('useBuildWorkers', () => {
  it('never returns build A workers in the first render after switching to session B / build B', async () => {
    request.mockImplementation((sessionId: string) =>
      sessionId === 'session-a'
        ? Promise.resolve({ jobs: [wireRow({ label: 'alpha-name', purpose: 'Alpha build purpose' })] })
        : new Promise(() => {}) // build B has not answered yet
    )
    const renders: Array<{ runId: string; jobs: null | RelayJob[] }> = []

    const { rerender } = renderHook(
      ({ runId, sessionId }: { runId: string; sessionId: string }) => {
        const jobs = useBuildWorkers(sessionId, runId)
        renders.push({ jobs, runId })

        return jobs
      },
      { initialProps: { runId: RUN_A, sessionId: 'session-a' } }
    )
    await flush()

    expect(renders.at(-1)?.jobs?.map(job => job.label)).toEqual(['alpha-name'])

    const before = renders.length
    rerender({ runId: RUN_B, sessionId: 'session-b' })
    const afterSwitch = renders.slice(before)

    expect(afterSwitch.length).toBeGreaterThan(0)
    expect(afterSwitch[0].runId).toBe(RUN_B)

    for (const { jobs } of afterSwitch) {
      const text = JSON.stringify(jobs)
      expect(text).not.toContain('alpha-name')
      expect(text).not.toContain('Alpha build purpose')
    }

    expect(afterSwitch[0].jobs).toBeNull()
  })

  it('drops a late answer for the previous build', async () => {
    let answerA: (value: unknown) => void = () => {}
    request.mockImplementation((sessionId: string) =>
      sessionId === 'session-a'
        ? new Promise(resolve => { answerA = resolve })
        : Promise.resolve({ jobs: [wireRow({ label: 'beta-name', purpose: 'Beta build purpose' })] })
    )

    const { rerender, result } = renderHook(
      ({ runId, sessionId }: { runId: string; sessionId: string }) => useBuildWorkers(sessionId, runId),
      { initialProps: { runId: RUN_A, sessionId: 'session-a' } }
    )
    rerender({ runId: RUN_B, sessionId: 'session-b' })
    await flush()
    await act(async () => { answerA({ jobs: [wireRow({ label: 'alpha-name' })] }) })
    await flush()

    expect(result.current?.map(job => job.label)).toEqual(['beta-name'])
  })
})
