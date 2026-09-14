import { beforeEach, describe, expect, it, vi } from 'vitest'

const { refreshSupportedSessionControlAfterTurn } = vi.hoisted(() => ({
  refreshSupportedSessionControlAfterTurn: vi.fn(async () => undefined)
}))

const { clearActiveSessionTodos, clearAllPrompts, clearClarifyRequest, playCompletionSound } = vi.hoisted(() => ({
  clearActiveSessionTodos: vi.fn(),
  clearAllPrompts: vi.fn(),
  clearClarifyRequest: vi.fn(),
  playCompletionSound: vi.fn()
}))

vi.mock('@/store/session-control', () => ({ refreshSupportedSessionControlAfterTurn }))
vi.mock('@/store/prompts', () => ({ clearAllPrompts }))
vi.mock('@/store/todos', () => ({ clearActiveSessionTodos }))
vi.mock('@/store/clarify', () => ({ clearClarifyRequest }))
vi.mock('@/lib/completion-sound', () => ({ playCompletionSound }))

beforeEach(() => {
  vi.clearAllMocks()
})

import type { GatewayEventName } from '@hermes/shared'

import { handleMessageStreamEvent } from './message-stream'
import type { GatewayEventContext } from './types'

function context(type: GatewayEventName): GatewayEventContext {
  return {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: 's1' },
      appendAssistantDelta: vi.fn(),
      appendReasoningDelta: vi.fn(),
      compactedTurnRef: { current: new Set() },
      completeAssistantMessage: vi.fn(),
      failAssistantMessage: vi.fn(),
      finalizeInterimAssistantMessage: vi.fn(),
      flushQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(async () => undefined),
      lastCwdInfoSessionRef: { current: null },
      nativeSubagentSessionsRef: { current: new Set() },
      queryClient: {} as GatewayEventContext['deps']['queryClient'],
      refreshHermesConfig: vi.fn(async () => undefined),
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: vi.fn(() => false),
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn(),
      upsertToolCall: vi.fn()
    },
    event: { type },
    explicitSid: 's1',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload: { text: 'completed' },
    scheduleConfigRefresh: vi.fn(),
    sessionId: 's1'
  }
}

describe('handleMessageStreamEvent session-control integration', () => {
  it('refreshes only after message.complete, through the store seam', () => {
    expect(handleMessageStreamEvent(context('message.delta'))).toBe(true)
    expect(refreshSupportedSessionControlAfterTurn).not.toHaveBeenCalled()

    expect(handleMessageStreamEvent(context('message.complete'))).toBe(true)
    expect(refreshSupportedSessionControlAfterTurn).toHaveBeenCalledTimes(1)
    expect(refreshSupportedSessionControlAfterTurn).toHaveBeenCalledWith('s1')
  })
})

describe('handleMessageStreamEvent background delivery contracts', () => {
  it('does not tear down foreground state or play completion sound for background complete', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'Background peer result',
      display_metadata: { direction: 'in', peer: 'planner' }
    } as unknown as GatewayEventContext['payload']

    handleMessageStreamEvent(ctx)

    expect(clearActiveSessionTodos).not.toHaveBeenCalled()
    expect(clearAllPrompts).not.toHaveBeenCalled()
    expect(playCompletionSound).not.toHaveBeenCalled()
  })

  it('tears down foreground state and plays completion sound for foreground complete', () => {
    const ctx = context('message.complete')

    handleMessageStreamEvent(ctx)

    expect(clearActiveSessionTodos).toHaveBeenCalledWith('s1')
    expect(clearAllPrompts).toHaveBeenCalledWith('s1')
    expect(playCompletionSound).toHaveBeenCalledWith('s1')
  })

  it('leaves busy false and is not refused when interrupted on background message.start', () => {
    const ctx = context('message.start')
    ctx.payload = { background: true, display_kind: 'peer_message' } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: true,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(currentState.busy).toBe(false)
    expect(currentState.awaitingResponse).toBe(false)
    expect(currentState.interrupted).toBe(true)
    expect(currentState.streamId).toBeTruthy()
  })

  it('appends background complete while interrupted (not dropped) and does not re-arm busy', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'Inbound peer task instructions\nLine 2',
      display_metadata: {
        direction: 'in',
        peer: 'planner'
      }
    } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: true,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(ctx.deps.completeAssistantMessage).not.toHaveBeenCalled()
    expect(currentState.busy).toBe(false)
    expect(currentState.interrupted).toBe(true)
    expect(currentState.messages).toHaveLength(1)

    const appended = currentState.messages[0]
    expect(appended.role).toBe('system')
    expect((appended.parts[0] as { text?: string })?.text).toContain('↘ from planner')
    expect(appended.asyncResult).toBe('Inbound peer task instructions\nLine 2')
  })

  it('appends sdk_background_result as assistant message without dropping when interrupted', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'sdk_background_result',
      text: 'Background execution output'
    } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: true,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(currentState.messages).toHaveLength(1)
    const appended = currentState.messages[0]
    expect(appended.role).toBe('assistant')
    expect((appended.parts[0] as { text?: string })?.text).toBe('Background execution output')
  })

  it('marks unread-finished for non-focused session on background complete', async () => {
    const { $unreadFinishedSessionIds, $selectedStoredSessionId } = await import('@/store/session')

    $unreadFinishedSessionIds.set([])
    $selectedStoredSessionId.set('other-session')

    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'New update',
      display_metadata: { direction: 'in', peer: 'worker' }
    } as unknown as GatewayEventContext['payload']

    handleMessageStreamEvent(ctx)
    expect($unreadFinishedSessionIds.get()).toContain('s1')

    // But when the session is focused, it does not mark unread
    $unreadFinishedSessionIds.set([])
    $selectedStoredSessionId.set('s1')

    handleMessageStreamEvent(ctx)
    expect($unreadFinishedSessionIds.get()).not.toContain('s1')
  })
})
