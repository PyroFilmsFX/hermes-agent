import { atom } from 'nanostores'

// D62 "continue, don't replay": the error card's Retry continues the session's
// interrupted work in the SAME Claude session (gateway `session.continue`). It
// never re-sends the failed turn's prompt. The card shows "interrupted,
// continuing…" while the continuation runs, then collapses to a quiet
// "recovered, continued" marker (Details kept) once it produces output.

export type ContinuePhase = 'continuing' | 'recovered'

export type ContinueEvent = 'complete' | 'failed' | 'progress' | 'retry'

export interface ContinueCard {
  phase: ContinuePhase
  sessionId: string
}

export const $continueCards = atom<Record<string, ContinueCard>>({})

export type GatewayRequest = (method: string, params: Record<string, unknown>) => Promise<unknown>

/** Pure phase table: retry → continuing; output/completion → recovered; a failed
 *  continue drops back to the plain error card (Retry offered again). */
export function nextContinuePhase(phase: ContinuePhase | undefined, event: ContinueEvent): ContinuePhase | undefined {
  if (event === 'retry') {
    return phase === 'recovered' ? phase : 'continuing'
  }

  if (phase !== 'continuing') {
    return phase
  }

  return event === 'failed' ? undefined : 'recovered'
}

function setPhase(messageId: string, sessionId: string, phase: ContinuePhase | undefined): void {
  const { [messageId]: _dropped, ...rest } = $continueCards.get()

  $continueCards.set(phase ? { ...rest, [messageId]: { phase, sessionId } } : rest)
}

/** Retry = continue. Calls `session.continue` for the card's session; never
 *  `prompt.submit` / a reload of the old prompt. */
export async function continueTurn(sessionId: string, messageId: string, request: GatewayRequest): Promise<void> {
  const current = $continueCards.get()[messageId]?.phase

  setPhase(messageId, sessionId, nextContinuePhase(current, 'retry'))

  try {
    await request('session.continue', { session_id: sessionId })
  } catch (error) {
    setPhase(messageId, sessionId, nextContinuePhase('continuing', 'failed'))

    throw error
  }
}

/** The continuation streamed output or completed for this session. */
export function noteContinuationProgress(sessionId: string, event: 'complete' | 'progress' = 'progress'): void {
  const cards = $continueCards.get()
  let changed = false
  const next: Record<string, ContinueCard> = {}

  for (const [messageId, card] of Object.entries(cards)) {
    const phase = card.sessionId === sessionId ? nextContinuePhase(card.phase, event) : card.phase

    changed ||= phase !== card.phase

    if (phase) {
      next[messageId] = { ...card, phase }
    }
  }

  if (changed) {
    $continueCards.set(next)
  }
}
