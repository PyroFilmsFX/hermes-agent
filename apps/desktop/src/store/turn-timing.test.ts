import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $sessions } from '@/store/session'
import { $sessionStates, $workingSessionIds, clearAllSessionStates } from '@/store/session-states'

import {
  $backendTurnStartedAtByStoredId,
  $hasActiveElapsedTurns,
  $turnStartedAtByStoredId,
  $turnTimerNow,
  clearAllBackendTurnStartedAt,
  clearBackendTurnStartedAt,
  resetTurnTimerForTest,
  setBackendTurnStartedAt
} from './turn-timing'

describe('turn-timing store', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    clearAllSessionStates()
    clearAllBackendTurnStartedAt()
    $sessions.set([])
    resetTurnTimerForTest()
  })

  afterEach(() => {
    resetTurnTimerForTest()
    vi.useRealTimers()
  })

  it('falls back to ClientSessionState.turnStartedAt when no backend start is reported', () => {
    $sessions.set([
      {
        id: 'stored-1',
        title: 'Chat 1',
        started_at: 1000,
        ended_at: null,
        is_active: true,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: ''
      }
    ])
    $sessionStates.set({
      'runtime-1': {
        ...createClientSessionState('stored-1'),
        busy: true,
        turnLive: true,
        turnStartedAt: 12345000
      }
    })

    const map = $turnStartedAtByStoredId.get()
    expect(map['stored-1']).toBe(12345000)
  })

  it('prefers backend turn_started_at over ClientSessionState.turnStartedAt', () => {
    $sessions.set([
      {
        id: 'stored-1',
        title: 'Chat 1',
        started_at: 1000,
        ended_at: null,
        is_active: true,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: '',
        turn_started_at: 1700000000 // epoch seconds
      }
    ])
    $sessionStates.set({
      'runtime-1': {
        ...createClientSessionState('stored-1'),
        busy: true,
        turnLive: true,
        turnStartedAt: 1600000000000 // older client fallback
      }
    })

    // From $sessions:
    expect($turnStartedAtByStoredId.get()['stored-1']).toBe(1700000000000)

    // Explicit setBackendTurnStartedAt overrides both:
    setBackendTurnStartedAt('stored-1', 1700000050)
    expect($turnStartedAtByStoredId.get()['stored-1']).toBe(1700000050000)
  })

  it('reports turn start for an unopened session with no $sessionStates entry', () => {
    $sessions.set([
      {
        id: 'unopened-1',
        title: 'Background task',
        started_at: 1000,
        ended_at: null,
        is_active: true,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: ''
      }
    ])
    expect($sessionStates.get()['unopened-1']).toBeUndefined()

    setBackendTurnStartedAt('unopened-1', 1700001000)
    expect($turnStartedAtByStoredId.get()['unopened-1']).toBe(1700001000000)
  })

  it('shared ticking atom advances every 15s only when at least one row has an active elapsed turn', () => {
    $sessions.set([
      {
        id: 'active-1',
        title: 'Active Chat',
        started_at: 1000,
        ended_at: null,
        is_active: true,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: ''
      }
    ])
    // Initially idle, no turns running:
    expect($hasActiveElapsedTurns.get()).toBe(false)
    const initialTime = Date.now()

    vi.advanceTimersByTime(30_000)
    // No timer running, so $turnTimerNow has not ticked:
    expect($turnTimerNow.get()).toBe(initialTime)

    // Arm a live turn:
    const armedTime = Date.now()
    $sessionStates.set({
      'rt-active': {
        ...createClientSessionState('active-1'),
        busy: true,
        turnLive: true,
        turnStartedAt: armedTime
      }
    })
    expect($hasActiveElapsedTurns.get()).toBe(true)

    vi.advanceTimersByTime(15_000)
    expect($turnTimerNow.get()).toBe(armedTime + 15_000)

    vi.advanceTimersByTime(15_000)
    expect($turnTimerNow.get()).toBe(armedTime + 30_000)

    // Turn completes:
    $sessionStates.set({})
    expect($hasActiveElapsedTurns.get()).toBe(false)

    // Timer is disarmed:
    vi.advanceTimersByTime(30_000)
    expect($turnTimerNow.get()).toBe(armedTime + 30_000)
  })

  it('the compression-rotation leftover does not keep the row working or the timer running', () => {
    $sessions.set([
      {
        id: 'stored-new',
        _lineage_root_id: 'root-1',
        _lineage_ids: ['stored-old', 'stored-new'],
        title: 'Active Chat',
        started_at: 1000,
        ended_at: null,
        is_active: true,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: ''
      },
      {
        id: 'stored-old',
        _lineage_root_id: 'root-1',
        _lineage_ids: ['stored-old', 'stored-new'],
        title: 'Active Chat',
        started_at: 1000,
        ended_at: null,
        is_active: false,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: ''
      }
    ])

    // Turn started under old stored id:
    setBackendTurnStartedAt('stored-old', 1700000000)

    // Compression rotates stored id, and end event clears the new id:
    clearBackendTurnStartedAt('stored-new')

    // Leftover old key should be dropped for all lineage aliases:
    expect($backendTurnStartedAtByStoredId.get()['stored-old']).toBeUndefined()

    // Row must not be considered working, and timer must not run:
    expect($workingSessionIds.get().includes('stored-new')).toBe(false)
    expect($hasActiveElapsedTurns.get()).toBe(false)

    // Even if a timestamp somehow remained in $backendTurnStartedAtByStoredId without busy:
    setBackendTurnStartedAt('stored-old', 1700000000)
    expect($workingSessionIds.get().includes('stored-new')).toBe(false)
    expect($hasActiveElapsedTurns.get()).toBe(false)
  })

  it('a SessionInfo with a stale turn_started_at and no busy is NOT working', () => {
    $sessions.set([
      {
        id: 'stale-turn-1',
        title: 'Idle Chat',
        started_at: 1000,
        ended_at: null,
        is_active: false,
        last_active: 1000,
        message_count: 1,
        input_tokens: 0,
        output_tokens: 0,
        tool_call_count: 0,
        preview: '',
        source: '',
        model: '',
        turn_started_at: 1700000000
      }
    ])

    expect($workingSessionIds.get().includes('stale-turn-1')).toBe(false)
  })
})
