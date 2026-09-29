import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $continueCards, continueTurn, nextContinuePhase, noteContinuationProgress } from './turn-continue'

describe('D62 continue card lifecycle', () => {
  beforeEach(() => $continueCards.set({}))

  it('Retry calls session.continue for the session and never re-sends the prompt', async () => {
    const request = vi.fn().mockResolvedValue({ status: 'continuing' })

    await continueTurn('sess-1', 'msg-err', request)

    expect(request).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledWith('session.continue', { session_id: 'sess-1' })
    expect(request.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(false)
    expect($continueCards.get()['msg-err']).toEqual({ phase: 'continuing', sessionId: 'sess-1' })
  })

  it('continuing collapses to the recovered marker on output, and only for that session', async () => {
    await continueTurn('sess-1', 'msg-err', vi.fn().mockResolvedValue({}))
    await continueTurn('sess-2', 'msg-other', vi.fn().mockResolvedValue({}))

    noteContinuationProgress('sess-1')

    expect($continueCards.get()['msg-err']?.phase).toBe('recovered')
    expect($continueCards.get()['msg-other']?.phase).toBe('continuing')

    noteContinuationProgress('sess-1', 'complete')
    expect($continueCards.get()['msg-err']?.phase).toBe('recovered')
  })

  it('a refused continue restores the plain error card', async () => {
    const request = vi.fn().mockRejectedValue(new Error('session is busy'))

    await expect(continueTurn('sess-1', 'msg-err', request)).rejects.toThrow('busy')
    expect($continueCards.get()['msg-err']).toBeUndefined()
  })

  it('phase table', () => {
    expect(nextContinuePhase(undefined, 'retry')).toBe('continuing')
    expect(nextContinuePhase('continuing', 'progress')).toBe('recovered')
    expect(nextContinuePhase('continuing', 'failed')).toBeUndefined()
    expect(nextContinuePhase(undefined, 'progress')).toBeUndefined()
    expect(nextContinuePhase('recovered', 'retry')).toBe('recovered')
  })
})
