import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { useStoreSelector } from '@/lib/use-session-slice'
import { $gatewayState } from '@/store/session'
import { knownOwnerForSession, requestForOwnedSession } from '@/store/session-states'
import { $conductorBuildBySession, reconcileConductorBuildSnapshot, type ConductorBuildSnapshot } from '@/store/conductor-build'

const ARMED_POLL_MS = 2_000
const IDLE_POLL_MS = 10_000

export const rejectUnownedConductorBuildRequest = async <T>(): Promise<T> => {
  throw new Error('Conductor build owner unavailable')
}

export function useConductorBuild(sessionId: string | null) {
  const gatewayState = useStore($gatewayState)
  const paneVisible = usePaneVisible()
  const build = useStoreSelector($conductorBuildBySession, builds =>
    sessionId ? builds[sessionId] ?? null : null
  )

  useEffect(() => {
    if (!sessionId || !paneVisible) {
      return
    }

    let cancelled = false
    let pending = false
    let failures = 0
    const owner = JSON.stringify(knownOwnerForSession(sessionId))

    const refresh = async () => {
      if (cancelled || pending || failures >= 3) {
        return
      }
      pending = true
      try {
        const snapshot = await requestForOwnedSession<ConductorBuildSnapshot>(
          sessionId,
          rejectUnownedConductorBuildRequest,
          'conductor_build.get',
          { session_id: sessionId }
        )
        if (!cancelled && owner === JSON.stringify(knownOwnerForSession(sessionId))) {
          reconcileConductorBuildSnapshot(sessionId, snapshot)
        }
        failures = 0
      } catch {
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
    }, build ? ARMED_POLL_MS : IDLE_POLL_MS)
    const retry = () => {
      failures = 0
      void refresh()
    }
    window.addEventListener('focus', retry)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      window.removeEventListener('focus', retry)
    }
  }, [sessionId, gatewayState, paneVisible, build?.run_id])
}
