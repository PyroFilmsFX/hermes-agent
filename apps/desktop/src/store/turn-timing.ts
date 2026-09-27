import { atom, computed } from 'nanostores'

import { $sessions, lineageAliases } from '@/store/session'
import { $sessionDotStateById, hasLiveTurn } from '@/store/session-dot-state'
import {
  $backendTurnStartedAtByStoredId,
  $sessionStates,
  clearAllBackendTurnStartedAt,
  clearBackendTurnStartedAt,
  setBackendTurnStartedAt
} from '@/store/session-states'

export {
  $backendTurnStartedAtByStoredId,
  clearAllBackendTurnStartedAt,
  clearBackendTurnStartedAt,
  setBackendTurnStartedAt
}

/**
 * Maps stored session IDs to current turn start timestamp (epoch ms).
 * Prefers backend-reported `turn_started_at`; falls back to `ClientSessionState.turnStartedAt`
 * mapped to stored IDs via lineageAliases.
 */
export const $turnStartedAtByStoredId = computed(
  [$backendTurnStartedAtByStoredId, $sessionStates, $sessions],
  (backendStarts, states, sessions) => {
    const result: Record<string, number> = {}

    // 1. Fallback from ClientSessionState.turnStartedAt ($sessionStates keyed by runtime ID)
    for (const [runtimeId, state] of Object.entries(states)) {
      if (typeof state.turnStartedAt !== 'number' || state.turnStartedAt <= 0) {
        continue
      }

      const storedId = state.storedSessionId ?? runtimeId

      for (const alias of lineageAliases(storedId, sessions)) {
        result[alias] = state.turnStartedAt
      }
    }

    // 2. Check SessionInfo in $sessions for turn_started_at
    for (const sess of sessions) {
      if (typeof sess.turn_started_at === 'number' && sess.turn_started_at > 0) {
        const ms = sess.turn_started_at > 1e11 ? sess.turn_started_at : sess.turn_started_at * 1000

        for (const alias of lineageAliases(sess.id, sessions)) {
          result[alias] = ms
        }
      }
    }

    // 3. Prefer backend turn_started_at from $backendTurnStartedAtByStoredId
    for (const [id, startedAt] of Object.entries(backendStarts)) {
      if (typeof startedAt === 'number' && startedAt > 0) {
        const ms = startedAt > 1e11 ? startedAt : startedAt * 1000

        for (const alias of lineageAliases(id, sessions)) {
          result[alias] = ms
        }
      }
    }

    return result
  }
)

/** Whether at least one live session has a live turn with a known start time. */
export const $hasActiveElapsedTurns = computed(
  [$sessionDotStateById, $turnStartedAtByStoredId],
  (dotStates, turnStarts) => {
    for (const [id, startedAt] of Object.entries(turnStarts)) {
      if (typeof startedAt === 'number' && startedAt > 0) {
        const dot = dotStates[id]

        if (dot && hasLiveTurn(dot)) {
          return true
        }
      }
    }

    return false
  }
)

/** Shared ticking atom (15 s interval, started only while at least one row shows elapsed). */
export const $turnTimerNow = atom<number>(Date.now())

let timerInterval: ReturnType<typeof setInterval> | null = null

export function syncTurnTimer(hasActive: boolean): void {
  if (hasActive) {
    if (!timerInterval) {
      $turnTimerNow.set(Date.now())
      timerInterval = setInterval(() => {
        $turnTimerNow.set(Date.now())
      }, 15_000)
    }
  } else {
    if (timerInterval) {
      clearInterval(timerInterval)
      timerInterval = null
    }
  }
}

// Automatically sync the ticking timer when active elapsed turns presence changes.
$hasActiveElapsedTurns.subscribe(hasActive => {
  syncTurnTimer(hasActive)
})

export function resetTurnTimerForTest(): void {
  if (timerInterval) {
    clearInterval(timerInterval)
    timerInterval = null
  }

  $turnTimerNow.set(Date.now())
}
