import { useEffect, useState } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { relayJobsFromPayload, type RelayJob, sameRelayJob } from '@/store/composer-status'
import { requestForOwnedSession } from '@/store/session-states'

const POLL_MS = 5_000

const rejectUnownedBuildWorkersRequest = async <T>(): Promise<T> => {
  throw new Error('Build workers owner unavailable')
}

const buildKey = (sessionId: string, runId: string) => `${sessionId}\u0000${runId}`

/**
 * Every worker the armed build owns (`relay_jobs.list {scope: "build"}`), not limited to the
 * composer's 10-minute window. Polled every 5 s while the pane is visible; `null` until the
 * first answer so the pane never claims "no workers" before it knows. The stored list is keyed
 * by (session, run): a render for any other build gets `null`, never the previous build's rows.
 */
export function useBuildWorkers(sessionId: null | string, runId: null | string): null | RelayJob[] {
  const paneVisible = usePaneVisible()
  const [stored, setStored] = useState<null | { jobs: RelayJob[]; key: string }>(null)
  const key = sessionId && runId ? buildKey(sessionId, runId) : null

  useEffect(() => {
    if (!sessionId || !runId || !paneVisible) {
      return
    }

    const requestKey = buildKey(sessionId, runId)

    let cancelled = false
    let pending = false
    let failures = 0

    const refresh = async () => {
      if (cancelled || pending || failures >= 3) {
        return
      }

      pending = true

      try {
        const snapshot = await requestForOwnedSession<{ jobs?: Array<Record<string, unknown>> }>(
          sessionId,
          rejectUnownedBuildWorkersRequest,
          'relay_jobs.list',
          { scope: 'build', session_id: sessionId }
        )

        if (!cancelled && Array.isArray(snapshot.jobs)) {
          const next = relayJobsFromPayload(snapshot.jobs)

          setStored(prev =>
            prev &&
            prev.key === requestKey &&
            prev.jobs.length === next.length &&
            prev.jobs.every((job, index) => sameRelayJob(job, next[index]))
              ? prev
              : { jobs: next, key: requestKey }
          )
        }

        failures = 0
      } catch {
        // An older backend rejects the scope param; keep the last good list.
        failures++
      } finally {
        pending = false
      }
    }

    void refresh()

    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void refresh()
      }
    }, POLL_MS)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [sessionId, runId, paneVisible])

  return key && stored?.key === key ? stored.jobs : null
}
